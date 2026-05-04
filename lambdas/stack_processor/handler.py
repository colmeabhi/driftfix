"""
Stack Processor Lambda

Invoked async by the Orchestrator — one invocation per affected stack per batch run.

Template source priority:
  1. GitHub repo (preserves parameters, comments, structure, separate param files)
  2. CloudFormation get_template (fallback if GitHub unavailable)

When GitHub is used, the directory next to the template file is scanned for
parameter files (parameters.json, params.yaml, prod.json, etc.) and they are
passed to the LLM alongside the template. The LLM returns only the files it
changed; unchanged files are not sent downstream.

# INTERIM: Using Google Gemini. TODO: Replace with AWS Bedrock.
"""

import base64
import json
import logging
import os
import re
import urllib.error
import urllib.request

import boto3
import time
import random
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

sts_client      = boto3.client("sts")
lambda_client   = boto3.client("lambda")
secretsmanager  = boto3.client("secretsmanager")
bedrock_runtime = boto3.client("bedrock-runtime", region_name=os.environ.get("AWS_REGION", "us-east-1"))

RECONCILIATIONS_TABLE   = os.environ["DYNAMODB_RECONCILIATIONS_TABLE"]
VALIDATOR_FUNCTION_NAME = os.environ["VALIDATOR_FUNCTION_NAME"]
BEDROCK_MODEL_ID        = os.environ.get("BEDROCK_MODEL_ID", "").strip()

# INTERIM: Gemini — remove when switching to Bedrock
GEMINI_API_KEY_SECRET_ARN = os.environ.get("GEMINI_API_KEY_SECRET_ARN", "")

_secret_cache = {}


def lambda_handler(event, context):
    tenant            = event["tenant"]
    stack_name        = event["stack_name"]
    cloudtrail_events = event["cloudtrail_events"]
    tenant_id         = tenant["tenant_id"]

    if not cloudtrail_events:
        logger.warning("Invoked with empty cloudtrail_events for stack=%s — nothing to do", stack_name)
        return

    logger.info("Processing stack=%s tenant=%s events=%d",
                stack_name, tenant_id, len(cloudtrail_events))

    creds  = assume_cross_account_role(tenant["role_arn"], tenant["external_id"], stack_name)
    region = cloudtrail_events[0].get("awsRegion", "us-east-1")

    github_token = _get_secret(tenant.get("github_token_secret_arn", ""))
    github_repo  = tenant.get("github_repo", "")

    # files = {repo_path: content} — template + any parameter files found alongside it
    # primary_path = the template file path (None if CFn fallback was used)
    files, primary_path = fetch_files(creds, stack_name, region, github_token, github_repo)

    # updated_files = only the files the LLM changed (subset of files keys)
    updated_files = invoke_bedrock(files, cloudtrail_events)

    invoke_validator(tenant, stack_name, cloudtrail_events[0],
                     files, updated_files, primary_path)


# ── Template + parameter file fetching ───────────────────────────────────────

def fetch_files(creds, stack_name, region, github_token, github_repo):
    """
    Return ({path: content}, primary_path).
    Tries GitHub first (name match, then content match); falls back to CloudFormation.
    primary_path is None when CFn fallback is used.
    """
    if github_token and github_repo:
        result = _fetch_github_files(github_token, github_repo, stack_name, creds, region)
        if result:
            files, primary_path = result
            logger.info("GitHub source: %s (+%d param file(s))",
                        primary_path, len(files) - 1)
            return files, primary_path
        logger.warning("GitHub fetch failed for stack '%s' — falling back to CloudFormation",
                       stack_name)

    content = _fetch_cfn_template(creds, stack_name, region)
    return {"template.yaml": content}, None


def _fetch_cfn_template(creds, stack_name, region):
    cfn  = boto3.client(
        "cloudformation", region_name=region,
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
    )
    body = cfn.get_template(StackName=stack_name, TemplateStage="Original")["TemplateBody"]
    return body if isinstance(body, str) else json.dumps(body, indent=2)


def _fetch_github_files(token, repo, stack_name, creds=None, region=None):
    """
    1. Fetch repo tree
    2. Find the template file by name matching, then content matching as fallback
    3. Scan the same directory for parameter files
    Returns ({path: content}, primary_path) or None.
    """
    owner, repo_name = repo.split("/", 1)

    try:
        info   = _github_request(token, "GET", f"/repos/{owner}/{repo_name}")
        branch = info["default_branch"]
        tree   = _github_request(token, "GET",
                     f"/repos/{owner}/{repo_name}/git/trees/{branch}?recursive=1")
    except Exception as e:
        logger.warning("GitHub tree fetch failed: %s", e)
        return None

    blobs = {
        item["path"]: item
        for item in tree.get("tree", [])
        if item["type"] == "blob"
    }
    yaml_blobs = [p for p in blobs if p.lower().endswith((".yaml", ".yml", ".json"))]

    # ── Find template file (name-based, then content-based fallback) ──────────
    template_path = _find_template(token, owner, repo_name, branch, yaml_blobs, stack_name)
    if not template_path and creds and region:
        template_path = _find_template_by_content(
            token, owner, repo_name, branch, yaml_blobs, creds, stack_name, region
        )
    if not template_path:
        return None

    template_content = _file_content(token, owner, repo_name, template_path, branch)
    if not template_content or not _is_cfn(template_content):
        return None

    files = {template_path: template_content}

    # ── Find parameter files in the same directory ────────────────────────────
    template_dir = template_path.rsplit("/", 1)[0] if "/" in template_path else ""
    for path in yaml_blobs:
        if path == template_path:
            continue
        file_dir = path.rsplit("/", 1)[0] if "/" in path else ""
        if file_dir != template_dir:
            continue
        filename = path.rsplit("/", 1)[-1].lower()
        if _looks_like_param_file(filename):
            content = _file_content(token, owner, repo_name, path, branch)
            if content and _is_param_file(content):
                files[path] = content
                logger.info("Parameter file found: %s", path)

    return files, template_path


_PARAM_NAME_HINTS = (
    "param", "parameter", "override", "variable", "var", "config",
    "dev", "prod", "staging", "test", "uat", "qa",
)

def _looks_like_param_file(filename):
    name = filename.rsplit(".", 1)[0].lower()
    return any(hint in name for hint in _PARAM_NAME_HINTS)


def _is_param_file(content):
    """True if content looks like a CFn parameter file (not a template)."""
    if _is_cfn(content):
        return False
    stripped = content.strip()
    if stripped.startswith("["):
        try:
            data = json.loads(stripped)
            return isinstance(data, list) and all(
                isinstance(i, dict) and "ParameterKey" in i for i in data[:3]
            )
        except Exception:
            return False
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
            return isinstance(data, dict) and all(
                isinstance(v, (str, int, float, bool)) for v in list(data.values())[:5]
            )
        except Exception:
            return False
    # Simple YAML key:value mapping
    lines = [ln for ln in stripped.splitlines() if ln.strip() and not ln.startswith("#")]
    return bool(lines) and all(
        re.match(r"^[A-Za-z][A-Za-z0-9]*\s*:\s*.+", ln) for ln in lines[:10]
    )


def _find_template(token, owner, repo_name, branch, blobs, stack_name):
    stack_norm = _norm(stack_name)
    alts = {stack_norm}
    for suffix in ("stack", "template", "cfn", "cloudformation"):
        if stack_norm.endswith(suffix):
            alts.add(stack_norm[:-len(suffix)])
    alts.discard("")

    exact, by_dir, partial = [], [], []
    for path in blobs:
        parts     = path.split("/")
        name_norm = _norm(parts[-1].rsplit(".", 1)[0])
        if name_norm in alts:
            exact.append(path)
        elif len(parts) >= 2 and _norm(parts[-2]) in alts:
            by_dir.append(path)
        elif any(a and (a in name_norm or name_norm in a) for a in alts):
            partial.append(path)

    for path in (exact + by_dir + partial):
        content = _file_content(token, owner, repo_name, path, branch)
        if content and _is_cfn(content):
            return path
    return None


def _find_template_by_content(token, owner, repo_name, branch, blobs, creds, stack_name, region):
    """
    Fallback: fetch the live CFn template body and find the repo file with the
    highest Jaccard similarity on resource logical IDs.  Handles repos where the
    directory name has no lexical relation to the stack name (e.g. drift-test-06
    → stacks/06-sns-sqs/template.yaml).
    """
    try:
        cfn_content = _fetch_cfn_template(creds, stack_name, region)
    except Exception as e:
        logger.warning("Content-match: CFn fetch failed: %s", e)
        return None

    known_ids = _extract_resource_ids(cfn_content)
    if not known_ids:
        return None

    # Only consider files whose basename looks like a CFn template entry-point
    standard_names = {"template.yaml", "template.yml", "main.yaml", "main.yml",
                      "stack.yaml", "stack.yml"}
    candidates = [p for p in blobs if p.rsplit("/", 1)[-1].lower() in standard_names]
    if not candidates:
        candidates = blobs  # fall back to all YAML/JSON blobs

    best_score, best_path = 0.0, None
    for path in candidates[:30]:
        content = _file_content(token, owner, repo_name, path, branch)
        if not content or not _is_cfn(content):
            continue
        score = _jaccard(known_ids, _extract_resource_ids(content))
        if score > best_score:
            best_score, best_path = score, path

    if best_score >= 0.3:
        logger.info("Content-based template match: %s (jaccard=%.2f)", best_path, best_score)
        return best_path

    logger.warning("Content-based match found no candidate above threshold (best=%.2f)", best_score)
    return None


def _extract_resource_ids(template_str):
    if not template_str:
        return set()
    if template_str.strip().startswith("{"):
        try:
            return set(json.loads(template_str).get("Resources", {}).keys())
        except Exception:
            return set()
    ids, in_resources = set(), False
    for line in template_str.splitlines():
        if re.match(r"^Resources\s*:", line):
            in_resources = True
            continue
        if in_resources:
            m = re.match(r"^  ([A-Za-z][A-Za-z0-9]*)\s*:", line)
            if m:
                ids.add(m.group(1))
            elif line and not line[0].isspace():
                break
    return ids


def _jaccard(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# ── GitHub API ────────────────────────────────────────────────────────────────

def _github_request(token, method, path, body=None):
    url  = f"https://api.github.com{path}"
    data = json.dumps(body).encode() if body else None
    req  = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "Authorization":        f"Bearer {token}",
            "Accept":               "application/vnd.github+json",
            "Content-Type":         "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def _file_content(token, owner, repo_name, path, branch):
    try:
        resp = _github_request(token, "GET",
                   f"/repos/{owner}/{repo_name}/contents/{path}?ref={branch}")
        return base64.b64decode(resp["content"]).decode("utf-8", errors="ignore")
    except Exception:
        return ""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_json(text):
    """
    Strip markdown fences and leading prose so json.loads always gets clean JSON.
    Handles: ```json ... ```, ``` ... ```, and 'Here is the JSON:\n{...}'.
    """
    text = text.strip()
    # Strip markdown fences
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\n?", "", text).rstrip("`").strip()
    # If there's still no leading brace/bracket, find the first one
    first_brace = min(
        (text.find(c) for c in ("{", "[") if text.find(c) != -1),
        default=-1,
    )
    if first_brace > 0:
        text = text[first_brace:]
    # Trim trailing prose after the final closing brace/bracket
    last_brace = max(text.rfind("}"), text.rfind("]"))
    if last_brace != -1:
        text = text[: last_brace + 1]
    return text


def _is_cfn(content):
    return bool(content) and (
        "AWSTemplateFormatVersion" in content
        or "Transform: AWS::Serverless" in content
        or ("Resources:" in content and "Type: AWS::" in content)
    )


def _norm(s):
    return re.sub(r"[-_\s]", "", s).lower()


def invoke_bedrock(files, cloudtrail_events):
    """
    Calls Bedrock converse with the original invoke_bedrock prompt (JSON output schema).
    Inputs:  files = {path: content}, cloudtrail_events = list of CloudTrail records.
    Returns: {path: updated_content} — same shape expected by invoke_validator.
    Model is read from BEDROCK_MODEL_ID env var.
    """
    if not BEDROCK_MODEL_ID:
        raise RuntimeError("BEDROCK_MODEL_ID env var is not set")

    # Use the primary (first) file as the template string for the prompt
    primary_path = next(iter(files))
    cfn_template = files[primary_path]

    evidence = {
        "region": cloudtrail_events[0].get("awsRegion", "unknown") if cloudtrail_events else "unknown",
        "event_count": len(cloudtrail_events),
        "events": cloudtrail_events[:50],
        "note": "Only use the evidence provided; do not invent changes.",
    }

    instructions = (
        "You are an Infrastructure as Code assistant.\n"
        "Given:\n"
        "1) ORIGINAL CloudFormation template\n"
        "2) EVIDENCE: CloudTrail write events for this stack\n\n"
        "Task: Produce an UPDATED CloudFormation template that matches the changes in EVIDENCE.\n"
        "Rules:\n"
        "- Only modify resources/properties directly supported by EVIDENCE.\n"
        "- Do not invent resources, properties, or values.\n"
        "- If evidence is insufficient, return the ORIGINAL template unchanged and set manual_review_required=true.\n"
        "- Output ONLY valid JSON with exactly these keys:\n"
        "  updated_template (string), remediation (object)\n"
        "- remediation must include: summary, root_cause, confidence (0..1), proposed_changes (list), "
        "evidence_ids (list), manual_review_required (bool), warnings (list).\n"
    )

    user_input = {
        "original_template": cfn_template,
        "evidence": evidence,
    }

    prompt = instructions + "\nINPUT:\n" + json.dumps(user_input)

    max_attempts = 4
    base_sleep = 0.6
    start = time.perf_counter()
    last_err = None

    for attempt in range(1, max_attempts + 1):
        try:
            resp = bedrock_runtime.converse(
                modelId=BEDROCK_MODEL_ID,
                messages=[{"role": "user", "content": [{"text": prompt}]}],
                inferenceConfig={"maxTokens": 8192, "temperature": 0.15},
            )

            # Extract text from converse response: output.message.content[].text
            msg = (resp or {}).get("output", {}).get("message") or {}
            blocks = msg.get("content") or []
            model_text = "".join(
                b["text"] for b in blocks
                if isinstance(b, dict) and isinstance(b.get("text"), str)
            ).strip()

            if not model_text:
                raise RuntimeError("Bedrock converse returned empty assistant text")

            # Strip markdown fences the model may add
            if model_text.startswith("```json"):
                model_text = model_text[len("```json"):].strip()
            if model_text.startswith("```"):
                model_text = model_text[len("```"):].strip()
            if model_text.endswith("```"):
                model_text = model_text[:-3].strip()

            result = json.loads(model_text)

            updated_template = result.get("updated_template")
            if not isinstance(updated_template, str) or not updated_template.strip():
                raise RuntimeError("Bedrock did not return a non-empty updated_template string")

            elapsed_ms = int((time.perf_counter() - start) * 1000)
            remediation = result.get("remediation", {})
            logger.info(
                "Bedrock converse ok model=%s attempt=%d latency_ms=%d "
                "manual_review=%s confidence=%s",
                BEDROCK_MODEL_ID, attempt, elapsed_ms,
                remediation.get("manual_review_required") if isinstance(remediation, dict) else None,
                remediation.get("confidence") if isinstance(remediation, dict) else None,
            )

            # Return as {path: content} dict to match the validator contract
            return {primary_path: updated_template}

        except ClientError as e:
            last_err = e
            code = e.response.get("Error", {}).get("Code", "Unknown")
            status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")

            retryable = code in {
                "ThrottlingException",
                "ModelNotReadyException",
                "ModelTimeoutException",
                "ServiceUnavailableException",
                "InternalServerException",
            } or (status is not None and status >= 500)

            if retryable and attempt < max_attempts:
                sleep_s = base_sleep * (2 ** (attempt - 1)) + random.random() * 0.2
                logger.warning("Bedrock retryable error code=%s status=%s attempt=%d sleep=%.2fs",
                               code, status, attempt, sleep_s)
                time.sleep(sleep_s)
                continue

            logger.error("Bedrock non-retryable error code=%s status=%s err=%s", code, status, str(e))
            raise

    raise RuntimeError(f"Bedrock failed after {max_attempts} attempts: {last_err}")


def assume_cross_account_role(role_arn, external_id, session_name):
    resp = sts_client.assume_role(
        RoleArn=role_arn,
        RoleSessionName=f"drift-{session_name[:32]}",
        ExternalId=external_id,
        DurationSeconds=3600,
    )
    return resp["Credentials"]


def _get_secret(secret_arn):
    if not secret_arn:
        return None
    if secret_arn not in _secret_cache:
        try:
            resp = secretsmanager.get_secret_value(SecretId=secret_arn)
            secret = json.loads(resp["SecretString"])
            _secret_cache[secret_arn] = secret.get("token") or secret.get("api_key")
        except Exception as e:
            logger.warning("Could not fetch secret %s: %s", secret_arn, e)
            return None
    return _secret_cache[secret_arn]


def invoke_validator(tenant, stack_name, cloudtrail_event,
                     original_files, updated_files, primary_path):
    payload = {
        "tenant_id":               tenant["tenant_id"],
        "event_id":                cloudtrail_event["eventID"],
        "stack_name":              stack_name,
        "original_files":          original_files,   # {path: content} — for context
        "updated_files":           updated_files,     # {path: content} — only changed files
        "primary_path":            primary_path,      # template file path (for PR title/description)
        "cloudtrail_event":        cloudtrail_event,
        "github_repo":             tenant.get("github_repo", ""),
        "github_token_secret_arn": tenant.get("github_token_secret_arn", ""),
        "retry_count":             0,
    }
    lambda_client.invoke(
        FunctionName=VALIDATOR_FUNCTION_NAME,
        InvocationType="Event",
        Payload=json.dumps(payload),
    )
