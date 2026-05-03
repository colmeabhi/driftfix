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

import json
import logging
import os
import re
import urllib.error
import urllib.request

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

sts_client     = boto3.client("sts")
lambda_client  = boto3.client("lambda")
secretsmanager = boto3.client("secretsmanager")

RECONCILIATIONS_TABLE   = os.environ["DYNAMODB_RECONCILIATIONS_TABLE"]
VALIDATOR_FUNCTION_NAME = os.environ["VALIDATOR_FUNCTION_NAME"]

# INTERIM: Gemini — remove when switching to Bedrock
GEMINI_API_KEY_SECRET_ARN = os.environ.get("GEMINI_API_KEY_SECRET_ARN", "")

_secret_cache = {}


def lambda_handler(event, context):
    tenant            = event["tenant"]
    stack_name        = event["stack_name"]
    cloudtrail_events = event["cloudtrail_events"]
    tenant_id         = tenant["tenant_id"]

    logger.info("Processing stack=%s tenant=%s events=%d",
                stack_name, tenant_id, len(cloudtrail_events))

    creds  = assume_cross_account_role(tenant["role_arn"], tenant["external_id"], stack_name)
    region = cloudtrail_events[0].get("awsRegion", "us-east-1")

    github_token = _get_secret(tenant.get("github_token_secret_arn", ""))
    github_repo  = tenant.get("github_repo", "")

    # files = {repo_path: content} — template + any parameter files found alongside it
    # primary_path = the template file path (None if CFn fallback was used)
    files, primary_path = fetch_files(creds, stack_name, region, github_token, github_repo)

    # updated_files = only the files Gemini changed (subset of files keys)
    updated_files = invoke_gemini(files, cloudtrail_events)  # INTERIM: swap for invoke_bedrock()

    invoke_validator(tenant, stack_name, cloudtrail_events[0],
                     files, updated_files, primary_path)


# ── Template + parameter file fetching ───────────────────────────────────────

def fetch_files(creds, stack_name, region, github_token, github_repo):
    """
    Return ({path: content}, primary_path).
    Tries GitHub first; falls back to CloudFormation.
    primary_path is None when CFn fallback is used.
    """
    if github_token and github_repo:
        result = _fetch_github_files(github_token, github_repo, stack_name)
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


def _fetch_github_files(token, repo, stack_name):
    """
    1. Fetch repo tree
    2. Find the template file by name matching
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

    # ── Find template file ────────────────────────────────────────────────────
    template_path = _find_template(token, owner, repo_name, branch, yaml_blobs, stack_name)
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


# ── INTERIM: Gemini ───────────────────────────────────────────────────────────
# TODO: Replace invoke_gemini() with invoke_bedrock().

def invoke_gemini(files, cloudtrail_events):
    """
    INTERIM: Pass all files to Gemini, get back only the changed ones.
    Replace with invoke_bedrock() when Bedrock model is decided.
    """
    from google import genai

    client = genai.Client(api_key=_get_secret(GEMINI_API_KEY_SECRET_ARN))

    files_block = "\n\n".join(
        f"=== FILE: {path} ===\n{content}"
        for path, content in files.items()
    )
    events_str = json.dumps(cloudtrail_events, indent=2, default=str)

    prompt = (
        "You are a CloudFormation expert. An engineer made manual changes to AWS resources "
        "outside of CloudFormation, causing infrastructure drift.\n\n"
        "The following files from the team's source repository make up the stack configuration. "
        "Preserve all structure, comments, and formatting exactly:\n\n"
        f"{files_block}\n\n"
        "Here are the CloudTrail events describing what was manually changed:\n"
        f"{events_str}\n\n"
        "Rules:\n"
        "- Update ONLY the parts affected by these changes\n"
        "- If a value corresponds to a parameter, update the parameters file instead of "
        "hardcoding the value in the template\n"
        "- If a new resource attribute is needed, add a Parameter + update the parameters file\n"
        "- Preserve all !Ref, !Sub, !GetAtt, !If, and other intrinsic functions\n"
        "- Return ONLY the files that changed, using this exact format with no other text:\n\n"
        "=== FILE: <path> ===\n"
        "<full updated file content>\n"
        "=== FILE: <path2> ===\n"
        "<full updated file content>\n\n"
        "Do not include files that did not change. No markdown fences, no explanation."
    )

    logger.info("Calling Gemini with %d file(s) (INTERIM)", len(files))
    response = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
    return _parse_multi_file_response(response.text.strip(), files)

# ── End INTERIM Gemini section ────────────────────────────────────────────────


def _parse_multi_file_response(raw, original_files):
    """
    Parse === FILE: path === delimited output from the LLM.
    Only accepts paths that exist in original_files (prevents path injection).
    """
    # Strip markdown fences that the LLM might add despite instructions
    raw = re.sub(r"^```\w*\s*", "", raw, flags=re.MULTILINE)
    raw = re.sub(r"```\s*$", "", raw, flags=re.MULTILINE)

    parts = re.split(r"=== FILE: (.+?) ===\s*\n", raw)
    # parts[0] = preamble (discard), then alternating path / content
    result = {}
    for i in range(1, len(parts) - 1, 2):
        path    = parts[i].strip()
        content = parts[i + 1].strip() if i + 1 < len(parts) else ""
        if path in original_files and content:
            result[path] = content

    if not result:
        logger.warning("LLM returned no parseable files — treating primary file as changed")
        primary = next(iter(original_files))
        result[primary] = raw.strip()

    return result


# ── Helpers ───────────────────────────────────────────────────────────────────

def _norm(s):
    return re.sub(r"[-_\s]", "", s).lower()


def _is_cfn(content):
    return bool(content) and (
        "AWSTemplateFormatVersion" in content
        or "Transform: AWS::Serverless" in content
        or ("Resources:" in content and "Type: AWS::" in content)
    )


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
    import base64
    try:
        resp = _github_request(token, "GET",
                   f"/repos/{owner}/{repo_name}/contents/{path}?ref={branch}")
        return base64.b64decode(resp["content"]).decode("utf-8", errors="ignore")
    except Exception:
        return None


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


def assume_cross_account_role(role_arn, external_id, session_name):
    resp = sts_client.assume_role(
        RoleArn=role_arn,
        RoleSessionName=f"drift-{session_name[:32]}",
        ExternalId=external_id,
        DurationSeconds=3600,
    )
    return resp["Credentials"]


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
