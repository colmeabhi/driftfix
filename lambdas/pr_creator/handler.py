"""
PR Creator Lambda

Reads the validated CloudFormation template from S3 and opens a pull request
in the customer's GitHub repository using a personal access token stored in
Secrets Manager.

Branch name is deterministic: drift/<tenant_id>/<event_id[:8]>
so retries never create duplicate branches or PRs.
"""

import json
import os
import hashlib
import boto3
import urllib.request
import urllib.error
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client("s3")
secretsmanager = boto3.client("secretsmanager")
dynamodb = boto3.resource("dynamodb")

GITHUB_TOKEN_SECRET_ARN = os.environ["GITHUB_TOKEN_SECRET_ARN"]
RECONCILIATIONS_TABLE = os.environ["DYNAMODB_RECONCILIATIONS_TABLE"]
AUDIT_BUCKET = os.environ.get("AUDIT_BUCKET", "")

_github_token_cache = None


def lambda_handler(event, context):
    tenant_id = event["tenant_id"]
    event_id = event["event_id"]
    github_repo = event["github_repo"]  # "owner/repo"
    cloudtrail_event = event["cloudtrail_event"]
    template_s3_key = event["template_s3_key"]

    logger.info("Creating PR for tenant=%s event=%s repo=%s", tenant_id, event_id, github_repo)

    template_body = fetch_template(template_s3_key)
    github_token = get_github_token()

    branch_name = f"drift/{tenant_id}/{event_id[:8]}"
    pr_url = open_pull_request(github_token, github_repo, branch_name, template_body, cloudtrail_event)

    update_reconciliation(tenant_id, event_id, pr_url)
    logger.info("PR opened: %s", pr_url)
    return {"pr_url": pr_url}


def fetch_template(s3_key):
    resp = s3.get_object(Bucket=AUDIT_BUCKET, Key=s3_key)
    return resp["Body"].read().decode("utf-8")


def get_github_token():
    global _github_token_cache
    if _github_token_cache:
        return _github_token_cache
    resp = secretsmanager.get_secret_value(SecretId=GITHUB_TOKEN_SECRET_ARN)
    secret = json.loads(resp["SecretString"])
    _github_token_cache = secret.get("token") or secret.get("github_token")
    return _github_token_cache


def github_request(token, method, path, body=None):
    url = f"https://api.github.com{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def open_pull_request(token, repo, branch_name, template_body, cloudtrail_event):
    owner, repo_name = repo.split("/", 1)

    # Get default branch SHA
    repo_info = github_request(token, "GET", f"/repos/{owner}/{repo_name}")
    default_branch = repo_info["default_branch"]
    branch_info = github_request(token, "GET", f"/repos/{owner}/{repo_name}/git/ref/heads/{default_branch}")
    base_sha = branch_info["object"]["sha"]

    # Create branch (idempotent — ignore 422 if it already exists)
    try:
        github_request(token, "POST", f"/repos/{owner}/{repo_name}/git/refs", {
            "ref": f"refs/heads/{branch_name}",
            "sha": base_sha,
        })
    except urllib.error.HTTPError as e:
        if e.code != 422:
            raise

    # Get current file SHA to update it
    event_name = cloudtrail_event.get("eventName", "unknown")
    resource = cloudtrail_event.get("requestParameters", {})
    file_path = _resolve_template_file_path(cloudtrail_event)

    try:
        file_info = github_request(token, "GET", f"/repos/{owner}/{repo_name}/contents/{file_path}?ref={branch_name}")
        file_sha = file_info["sha"]
    except urllib.error.HTTPError as e:
        if e.code == 404:
            file_sha = None
        else:
            raise

    import base64
    content_b64 = base64.b64encode(template_body.encode()).decode()
    commit_body = {
        "message": f"fix(drift): update template after {event_name}",
        "content": content_b64,
        "branch": branch_name,
    }
    if file_sha:
        commit_body["sha"] = file_sha

    github_request(token, "PUT", f"/repos/{owner}/{repo_name}/contents/{file_path}", commit_body)

    # Open PR (idempotent — check if one already exists for this branch)
    pr_body = {
        "title": f"[Drift] {event_name} detected — template updated",
        "body": build_pr_description(cloudtrail_event),
        "head": branch_name,
        "base": default_branch,
    }
    try:
        pr = github_request(token, "POST", f"/repos/{owner}/{repo_name}/pulls", pr_body)
        return pr["html_url"]
    except urllib.error.HTTPError as e:
        if e.code == 422:
            # PR already exists for this branch
            prs = github_request(token, "GET", f"/repos/{owner}/{repo_name}/pulls?head={owner}:{branch_name}&state=open")
            return prs[0]["html_url"] if prs else branch_name
        raise


def _resolve_template_file_path(cloudtrail_event):
    # TODO: store the file path per stack in tenant config; defaulting for now
    return "template.yaml"


def build_pr_description(cloudtrail_event):
    event_name = cloudtrail_event.get("eventName", "unknown")
    event_time = cloudtrail_event.get("eventTime", "unknown")
    user = cloudtrail_event.get("userIdentity", {}).get("arn", "unknown")
    return (
        f"## Drift Detected\n\n"
        f"| Field | Value |\n"
        f"|---|---|\n"
        f"| Event | `{event_name}` |\n"
        f"| Time | {event_time} |\n"
        f"| Actor | `{user}` |\n\n"
        f"This PR was automatically generated by the drift detector. "
        f"Review the changes, ensure they match the intended infrastructure state, then merge to reconcile."
    )


def update_reconciliation(tenant_id, event_id, pr_url):
    table = dynamodb.Table(RECONCILIATIONS_TABLE)
    table.update_item(
        Key={"tenant_id": tenant_id, "event_id": event_id},
        UpdateExpression="SET #s = :s, pr_url = :pr",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": "pr_opened", ":pr": pr_url},
    )
