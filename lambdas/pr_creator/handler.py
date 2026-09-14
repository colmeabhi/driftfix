"""
PR Creator Lambda

Reads all updated files from S3 (template + any changed parameter files)
and opens a single pull request that commits them all atomically using the
GitHub Git Data API (blob → tree → commit → update ref).

Branch name: drift/<tenant_id>/<event_id[:8]>  — deterministic, safe to retry.
"""

import base64
import json
import logging
import os
import re
import urllib.error
import urllib.request

import boto3

from reconciliation import STATUS_FAILED, STATUS_PR_OPENED, mark_events

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3             = boto3.client("s3")
secretsmanager = boto3.client("secretsmanager")
dynamodb       = boto3.resource("dynamodb")

GITHUB_TOKEN_SECRET_ARN = os.environ["GITHUB_TOKEN_SECRET_ARN"]  # fallback / admin token
RECONCILIATIONS_TABLE   = os.environ["DYNAMODB_RECONCILIATIONS_TABLE"]
AUDIT_BUCKET            = os.environ.get("AUDIT_BUCKET", "")

_token_cache = {}


def lambda_handler(event, context):
    tenant_id        = event["tenant_id"]
    event_id         = event["event_id"]
    event_ids        = event.get("event_ids") or [event_id]   # every event this PR resolves
    stack_name       = event.get("stack_name", "")
    github_repo      = event["github_repo"]
    cloudtrail_event = event["cloudtrail_event"]
    updated_s3_keys  = event["updated_s3_keys"]           # {repo_path: s3_key}
    remediation      = event.get("remediation") or {}
    review_reasons   = event.get("review_reasons") or []

    secret_arn = event.get("github_token_secret_arn") or GITHUB_TOKEN_SECRET_ARN

    logger.info("Creating PR for tenant=%s event=%s repo=%s files=%d needs_review=%s",
                tenant_id, event_id, github_repo, len(updated_s3_keys), bool(review_reasons))

    table = dynamodb.Table(RECONCILIATIONS_TABLE)
    try:
        # Fetch all updated file contents from S3
        files = {path: fetch_from_s3(s3_key) for path, s3_key in updated_s3_keys.items()}

        token       = get_github_token(secret_arn)
        branch_name = f"drift/{tenant_id}/{event_id[:8]}"

        pr_url = open_pull_request(
            token, github_repo, branch_name, files,
            title=build_pr_title(cloudtrail_event, stack_name, review_reasons),
            description=build_pr_description(cloudtrail_event, stack_name, files,
                                             remediation, review_reasons, len(event_ids)),
            commit_message=build_commit_message(cloudtrail_event, files),
        )
    except Exception as e:
        # Record the failure so the Orchestrator retries these events on its
        # next run, then re-raise so the invocation is reported as an error.
        try:
            mark_events(table, tenant_id, event_ids, STATUS_FAILED, error=f"pr_creator: {e}")
        except Exception as mark_err:
            logger.error("Could not mark events failed: %s", mark_err)
        raise

    mark_events(table, tenant_id, event_ids, STATUS_PR_OPENED,
                pr_url=pr_url,
                needs_review=bool(review_reasons),
                confidence=_confidence(remediation))
    logger.info("PR opened: %s", pr_url)
    return {"pr_url": pr_url}


# ── GitHub token ──────────────────────────────────────────────────────────────

def get_github_token(secret_arn):
    if secret_arn not in _token_cache:
        resp   = secretsmanager.get_secret_value(SecretId=secret_arn)
        secret = json.loads(resp["SecretString"])
        _token_cache[secret_arn] = secret.get("token") or secret.get("github_token")
    return _token_cache[secret_arn]


# ── GitHub API ────────────────────────────────────────────────────────────────

def github_request(token, method, path, body=None):
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


# ── PR creation ───────────────────────────────────────────────────────────────

def open_pull_request(token, repo, branch_name, files, title, description, commit_message):
    """
    Create branch, commit all files atomically via Git Data API, open PR.
    files = {repo_path: content}
    """
    owner, repo_name = repo.split("/", 1)

    # Get default branch + its latest commit SHA
    repo_info      = github_request(token, "GET", f"/repos/{owner}/{repo_name}")
    default_branch = repo_info["default_branch"]
    ref_info       = github_request(token, "GET",
                         f"/repos/{owner}/{repo_name}/git/ref/heads/{default_branch}")
    base_commit_sha = ref_info["object"]["sha"]
    # Create branch (idempotent — ignore 422 if already exists)
    try:
        github_request(token, "POST", f"/repos/{owner}/{repo_name}/git/refs", {
            "ref": f"refs/heads/{branch_name}",
            "sha": base_commit_sha,
        })
    except urllib.error.HTTPError as e:
        if e.code != 422:
            raise

    # ── Atomic multi-file commit via Git Data API ─────────────────────────────
    # Create one blob per file
    tree_items = []
    for path, content in files.items():
        blob = github_request(token, "POST", f"/repos/{owner}/{repo_name}/git/blobs", {
            "content":  base64.b64encode(content.encode()).decode(),
            "encoding": "base64",
        })
        tree_items.append({
            "path": path,
            "mode": "100644",
            "type": "blob",
            "sha":  blob["sha"],
        })
        logger.info("Blob created for %s", path)

    # Create tree on top of the branch's current tree
    branch_ref    = github_request(token, "GET",
                        f"/repos/{owner}/{repo_name}/git/ref/heads/{branch_name}")
    branch_commit = github_request(token, "GET",
                        f"/repos/{owner}/{repo_name}/git/commits/{branch_ref['object']['sha']}")
    branch_tree   = branch_commit["tree"]["sha"]

    new_tree = github_request(token, "POST", f"/repos/{owner}/{repo_name}/git/trees", {
        "base_tree": branch_tree,
        "tree":      tree_items,
    })

    # Create commit
    new_commit = github_request(token, "POST", f"/repos/{owner}/{repo_name}/git/commits", {
        "message": commit_message,
        "tree":    new_tree["sha"],
        "parents": [branch_ref["object"]["sha"]],
    })

    # Move branch ref forward
    github_request(token, "PATCH",
        f"/repos/{owner}/{repo_name}/git/refs/heads/{branch_name}",
        {"sha": new_commit["sha"], "force": True},
    )

    # Open PR (idempotent)
    pr_body_text = {
        "title": title,
        "body":  description,
        "head":  branch_name,
        "base":  default_branch,
    }
    try:
        pr = github_request(token, "POST", f"/repos/{owner}/{repo_name}/pulls", pr_body_text)
        return pr["html_url"]
    except urllib.error.HTTPError as e:
        if e.code == 422:
            prs = github_request(token, "GET",
                f"/repos/{owner}/{repo_name}/pulls?head={owner}:{branch_name}&state=open")
            if prs:
                return prs[0]["html_url"]
            # PR was closed/merged between our create attempt and the lookup —
            # check merged PRs too before giving up
            prs_closed = github_request(token, "GET",
                f"/repos/{owner}/{repo_name}/pulls?head={owner}:{branch_name}&state=closed")
            if prs_closed:
                return prs_closed[0]["html_url"]
            raise RuntimeError(
                f"422 from GitHub but no open or closed PR found for branch {branch_name}"
            )
        raise


# ── Template path fallback discovery (used only when primary_path is None) ───

def _norm(s):
    return re.sub(r"[-_\s]", "", s).lower()


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


def _fetch_file_content(token, repo, path, branch):
    owner, repo_name = repo.split("/", 1)
    try:
        resp = github_request(token, "GET",
                   f"/repos/{owner}/{repo_name}/contents/{path}?ref={branch}")
        return base64.b64decode(resp["content"]).decode("utf-8", errors="ignore")
    except Exception:
        return ""


def _is_cfn_content(content):
    return bool(content) and (
        "AWSTemplateFormatVersion" in content
        or "Transform: AWS::Serverless" in content
        or ("Resources:" in content and "Type: AWS::" in content)
    )


def discover_primary_path(token, repo, stack_name, default_branch, template_body):
    """Fallback discovery when Stack Processor used the CFn template fallback."""
    owner, repo_name = repo.split("/", 1)
    try:
        tree = github_request(token, "GET",
                   f"/repos/{owner}/{repo_name}/git/trees/{default_branch}?recursive=1")
    except Exception as e:
        logger.warning("Repo tree unavailable: %s", e)
        return "template.yaml"

    blobs = [
        item["path"] for item in tree.get("tree", [])
        if item["type"] == "blob"
        and item["path"].lower().endswith((".yaml", ".yml", ".json"))
    ]

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

    known_ids    = _extract_resource_ids(template_body)
    name_matches = exact + by_dir + partial
    scan_pool    = name_matches or blobs[:20]

    scored = []
    for path in scan_pool[:20]:
        content = _fetch_file_content(token, repo, path, default_branch)
        if not _is_cfn_content(content):
            continue
        score = _jaccard(known_ids, _extract_resource_ids(content))
        scored.append((score, path))

    if scored:
        scored.sort(reverse=True)
        if scored[0][0] > 0:
            return scored[0][1]

    return "template.yaml"


# ── Helpers ───────────────────────────────────────────────────────────────────

def fetch_from_s3(s3_key):
    resp = s3.get_object(Bucket=AUDIT_BUCKET, Key=s3_key)
    return resp["Body"].read().decode("utf-8")


def build_commit_message(cloudtrail_event, files):
    event_name    = cloudtrail_event.get("eventName", "unknown")
    files_summary = ", ".join(p.rsplit("/", 1)[-1] for p in sorted(files))
    return f"fix(drift): update {files_summary} after {event_name}"


def build_pr_title(cloudtrail_event, stack_name, review_reasons):
    event_name = cloudtrail_event.get("eventName", "unknown")
    tag        = "[Drift – needs review]" if review_reasons else "[Drift]"
    return f"{tag} {event_name} detected on {stack_name}"


def build_pr_description(cloudtrail_event, stack_name, files,
                         remediation=None, review_reasons=None, event_count=1):
    """
    PR body: a warning banner when the change needs careful review, the facts
    of the drift, then the model's own explanation of what it changed and why.
    """
    remediation    = remediation or {}
    review_reasons = review_reasons or []
    event_name = cloudtrail_event.get("eventName", "unknown")
    event_time = cloudtrail_event.get("eventTime", "unknown")
    user       = (cloudtrail_event.get("userIdentity") or {}).get("arn", "unknown")
    confidence = _confidence(remediation)

    parts = []

    if review_reasons:
        banner = ["> [!WARNING]", "> **This change needs careful review before merging.**"]
        banner += [f"> - {reason}" for reason in review_reasons]
        parts.append("\n".join(banner))

    rows = [
        ("Stack", f"`{stack_name}`"),
        ("Event", f"`{event_name}`" + (f" (+{event_count - 1} more)" if event_count > 1 else "")),
        ("Time",  event_time),
        ("Actor", f"`{user}`"),
    ]
    if confidence is not None:
        rows.append(("Model confidence", f"{confidence:.0%}"))
    table = "\n".join(f"| {name} | {value} |" for name, value in rows)
    parts.append(f"## Drift Detected\n\n| Field | Value |\n|---|---|\n{table}")

    for heading, key in (("What changed", "summary"), ("Root cause", "root_cause")):
        text = remediation.get(key)
        if isinstance(text, str) and text.strip():
            parts.append(f"### {heading}\n\n{text.strip()}")

    for heading, key in (("Proposed changes", "proposed_changes"), ("Warnings", "warnings")):
        items = _bullets(remediation.get(key))
        if items:
            parts.append(f"### {heading}\n\n{items}")

    files_list = "\n".join(f"- `{p}`" for p in sorted(files))
    parts.append(f"**Files updated:**\n{files_list}")
    parts.append(
        "Review the changes, ensure they match the intended infrastructure state, "
        "then merge to reconcile."
    )
    return "\n\n".join(parts)


def _confidence(remediation):
    value = remediation.get("confidence")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _bullets(items, limit=20):
    """Render a model-supplied list as markdown bullets; anything else is ignored."""
    if not isinstance(items, list):
        return ""
    lines = []
    for item in items[:limit]:
        text = item if isinstance(item, str) else json.dumps(item, default=str)
        if text.strip():
            lines.append(f"- {text.strip()}")
    return "\n".join(lines)
