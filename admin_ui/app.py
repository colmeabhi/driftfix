"""
Drift Detector — Tenant Admin UI
Run: uvicorn app:app --reload --port 8000
Requires AWS credentials in environment (same profile used for Terraform).
"""

import os
import secrets
import textwrap
import boto3
from botocore.exceptions import ClientError
from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse

app = FastAPI()

REGION  = os.environ.get("AWS_REGION", "us-east-1")
TABLE   = os.environ.get("TENANTS_TABLE", "drift-detector-tenants")
PROJECT = os.environ.get("PROJECT", "drift-detector")

dynamodb        = boto3.resource("dynamodb", region_name=REGION)
secretsmanager  = boto3.client("secretsmanager", region_name=REGION)
table           = dynamodb.Table(TABLE)
SAAS_ACCOUNT_ID = boto3.client("sts", region_name=REGION).get_caller_identity()["Account"]

CROSS_ACCOUNT_ROLE_NAME = "drift-detector-cross-account"

# ── Helpers ───────────────────────────────────────────────────────────────────

def _all_tenants():
    items, kwargs = [], {}
    while True:
        resp = table.scan(**kwargs)
        items.extend(resp.get("Items", []))
        if not resp.get("LastEvaluatedKey"):
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return sorted(items, key=lambda x: x["tenant_id"])


def _cfn_template(tenant_id: str, external_id: str, cloudtrail_bucket: str) -> str:
    return textwrap.dedent(f"""\
        AWSTemplateFormatVersion: '2010-09-09'
        Description: >
          Drift Detector cross-account access for tenant {tenant_id}.
          Deploy this stack in AWS account {tenant_id} to allow the Drift Detector
          (account {SAAS_ACCOUNT_ID}) to monitor your CloudFormation stacks.

        Resources:

          DriftDetectorRole:
            Type: AWS::IAM::Role
            Properties:
              RoleName: {CROSS_ACCOUNT_ROLE_NAME}
              AssumeRolePolicyDocument:
                Version: '2012-10-17'
                Statement:
                  - Effect: Allow
                    Principal:
                      AWS: arn:aws:iam::{SAAS_ACCOUNT_ID}:root
                    Action: sts:AssumeRole
                    Condition:
                      StringEquals:
                        sts:ExternalId: {external_id}
              Policies:
                - PolicyName: DriftDetectorPolicy
                  PolicyDocument:
                    Version: '2012-10-17'
                    Statement:
                      - Sid: ReadCloudTrailLogs
                        Effect: Allow
                        Action:
                          - s3:GetObject
                          - s3:ListBucket
                        Resource:
                          - arn:aws:s3:::{cloudtrail_bucket}
                          - arn:aws:s3:::{cloudtrail_bucket}/*
                      - Sid: ResolveResourceTags
                        Effect: Allow
                        Action:
                          - cloudformation:GetTemplate
                          - ec2:DescribeTags
                          - s3:GetBucketTagging
                          - rds:DescribeDBInstances
                          - rds:DescribeDBClusters
                          - rds:ListTagsForResource
                          - lambda:GetFunction
                          - lambda:ListTags
                          - iam:ListRoleTags
                          - sns:ListTagsForResource
                          - sqs:ListQueueTags
                          - dynamodb:DescribeTable
                          - dynamodb:ListTagsOfResource
                        Resource: '*'

        Outputs:
          RoleArn:
            Value: !GetAtt DriftDetectorRole.Arn
            Description: Cross-account role ARN (for reference)
    """)


# ── Shared CSS + shell ────────────────────────────────────────────────────────

_CSS = """
  *, *::before, *::after { box-sizing: border-box; }
  body { font-family: system-ui, sans-serif; background: #f5f5f5; margin: 0; padding: 2rem; color: #1a1a1a; max-width: 960px; }
  h1 { margin: 0 0 .25rem; font-size: 1.4rem; }
  .subtitle { color: #666; font-size: .875rem; margin: 0 0 1.75rem; }
  h2 { margin: 0 0 1rem; font-size: .875rem; color: #555; text-transform: uppercase; letter-spacing: .06em; font-weight: 600; }
  a { color: #4f8ef7; text-decoration: none; }
  a:hover { text-decoration: underline; }

  .card { background: #fff; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,.1); padding: 1.5rem; margin-bottom: 1.5rem; }

  table { width: 100%; border-collapse: collapse; font-size: .875rem; }
  th { text-align: left; padding: .5rem .75rem; border-bottom: 2px solid #e5e5e5; color: #555; font-weight: 600; }
  td { padding: .55rem .75rem; border-bottom: 1px solid #f0f0f0; vertical-align: middle; }
  tr:last-child td { border-bottom: none; }
  .empty { color: #888; font-style: italic; }
  code { background: #f0f0f0; border-radius: 4px; padding: .1rem .35rem; font-size: .8rem; font-family: monospace; }

  label { display: flex; flex-direction: column; gap: .3rem; font-size: .875rem; font-weight: 500; }
  .hint { font-size: .75rem; color: #888; font-weight: 400; }
  input { padding: .5rem .65rem; border: 1px solid #ccc; border-radius: 6px; font-size: .875rem; width: 100%; }
  input:focus { outline: none; border-color: #4f8ef7; box-shadow: 0 0 0 2px rgba(79,142,247,.25); }
  .form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: .75rem 1rem; }
  .full { grid-column: 1 / -1; }

  .btn { padding: .5rem 1.1rem; border-radius: 6px; border: none; cursor: pointer; font-size: .875rem; font-weight: 500; display: inline-block; }
  .btn-primary { background: #4f8ef7; color: #fff; }
  .btn-primary:hover { background: #3a7aed; }
  .btn-secondary { background: #f0f0f0; color: #333; }
  .btn-secondary:hover { background: #e4e4e4; }
  .btn-danger { background: transparent; border: 1px solid #e55; color: #c33; padding: .3rem .7rem; font-size: .8rem; border-radius: 5px; cursor: pointer; }
  .btn-danger:hover { background: #fee; }
  .actions { margin-top: 1rem; display: flex; gap: .5rem; align-items: center; }

  .badge { display: inline-block; background: #e8f2ff; color: #2563eb; border-radius: 4px; padding: .1rem .45rem; font-size: .75rem; margin-left: .4rem; }
  .badge-green { background: #ecfdf5; color: #166534; }

  .flash { padding: .75rem 1rem; border-radius: 6px; margin-bottom: 1.25rem; font-size: .875rem; }
  .flash.ok  { background: #ecfdf5; color: #166534; border: 1px solid #bbf7d0; }
  .flash.err { background: #fef2f2; color: #991b1b; border: 1px solid #fecaca; }

  pre { background: #1e1e1e; color: #d4d4d4; border-radius: 8px; padding: 1.25rem; overflow-x: auto;
        font-size: .8rem; line-height: 1.6; margin: 0; white-space: pre; }
  .copy-bar { display: flex; justify-content: space-between; align-items: center; margin-bottom: .5rem; }
  .copy-bar span { font-size: .8rem; color: #555; font-weight: 500; }

  .step { display: flex; gap: 1rem; align-items: flex-start; margin-bottom: 1rem; }
  .step-num { background: #4f8ef7; color: #fff; border-radius: 50%; width: 1.6rem; height: 1.6rem;
              display: flex; align-items: center; justify-content: center; font-size: .75rem;
              font-weight: 700; flex-shrink: 0; margin-top: .1rem; }
  .step-body { font-size: .875rem; line-height: 1.5; }
  .step-body strong { display: block; margin-bottom: .2rem; }
"""

def _page(title, body):
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{title} — Drift Detector</title>
  <style>{_CSS}</style>
</head>
<body>
  <h1>Drift Detector</h1>
  <p class="subtitle">Tenant Administration</p>
  {body}
</body>
</html>"""


# ── Main page — tenant list ───────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(flash: str = "", flash_type: str = "ok"):
    tenants = _all_tenants()

    if tenants:
        rows = "".join(
            f"""<tr>
              <td><code>{t['tenant_id']}</code></td>
              <td>{t.get('github_repo', '—')}</td>
              <td>{t.get('cloudtrail_bucket', '—')}</td>
              <td>
                <form method="post" action="/tenants/{t['tenant_id']}/delete"
                      onsubmit="return confirm('Remove tenant {t['tenant_id']}?')">
                  <button class="btn-danger" type="submit">Remove</button>
                </form>
              </td>
            </tr>"""
            for t in tenants
        )
        table_html = f"""<table>
          <thead><tr>
            <th>Account ID</th><th>GitHub Repo</th><th>CloudTrail Bucket</th><th></th>
          </tr></thead>
          <tbody>{rows}</tbody>
        </table>"""
    else:
        table_html = '<p class="empty">No tenants yet.</p>'

    flash_html = f'<div class="flash {flash_type}">{flash}</div>' if flash else ""

    body = f"""
      {flash_html}
      <div class="card">
        <h2>Active Tenants <span class="badge">{len(tenants)}</span></h2>
        {table_html}
      </div>
      <a href="/register" class="btn btn-primary">+ Onboard New Tenant</a>
    """
    return _page("Tenants", body)


# ── Register — step 1: form ───────────────────────────────────────────────────

@app.get("/register", response_class=HTMLResponse)
async def register_form():
    body = """
      <div class="card">
        <h2>Onboard New Tenant</h2>
        <form method="post" action="/register">
          <div class="form-grid">
            <label>
              AWS Account ID
              <span class="hint">The customer's 12-digit account ID</span>
              <input name="tenant_id" placeholder="123456789012" required pattern="[0-9]{12}">
            </label>
            <label>
              GitHub Repo
              <span class="hint">Where their IaC templates live (owner/repo)</span>
              <input name="github_repo" placeholder="acme/infra" required>
            </label>
            <label class="full">
              CloudTrail S3 Bucket
              <span class="hint">The bucket in their account where CloudTrail writes logs</span>
              <input name="cloudtrail_bucket" placeholder="acme-cloudtrail-logs" required>
            </label>
            <label class="full">
              GitHub Personal Access Token
              <span class="hint">Fine-grained PAT with Contents + Pull Requests write access on their repo</span>
              <input name="github_pat" type="password" placeholder="github_pat_..." required>
            </label>
          </div>
          <div class="actions">
            <button class="btn btn-primary" type="submit">Generate Setup Template</button>
            <a href="/" class="btn btn-secondary">Cancel</a>
          </div>
        </form>
      </div>
    """
    return _page("Onboard Tenant", body)


# ── Register — step 2: save + show CFn template ───────────────────────────────

@app.post("/register", response_class=HTMLResponse)
async def register_submit(
    tenant_id: str = Form(...),
    github_repo: str = Form(...),
    cloudtrail_bucket: str = Form(...),
    github_pat: str = Form(...),
):
    tenant_id         = tenant_id.strip()
    github_repo       = github_repo.strip()
    cloudtrail_bucket = cloudtrail_bucket.strip()
    github_pat        = github_pat.strip()

    external_id = secrets.token_urlsafe(32)
    role_arn    = f"arn:aws:iam::{tenant_id}:role/{CROSS_ACCOUNT_ROLE_NAME}"

    # Store PAT in Secrets Manager; get back the ARN to save in DynamoDB
    secret_name = f"{PROJECT}/github-token-{tenant_id}"
    try:
        resp = secretsmanager.create_secret(
            Name=secret_name,
            SecretString=f'{{"token": "{github_pat}"}}',
            Description=f"GitHub PAT for drift-detector tenant {tenant_id}",
        )
        github_token_secret_arn = resp["ARN"]
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceExistsException":
            secretsmanager.put_secret_value(
                SecretId=secret_name,
                SecretString=f'{{"token": "{github_pat}"}}',
            )
            github_token_secret_arn = secretsmanager.describe_secret(
                SecretId=secret_name
            )["ARN"]
        else:
            body = f'<div class="flash err">Secrets Manager error: {e}</div><a href="/register" class="btn btn-secondary">Go back</a>'
            return _page("Error", body)

    try:
        table.put_item(
            Item={
                "tenant_id":              tenant_id,
                "role_arn":               role_arn,
                "external_id":            external_id,
                "cloudtrail_bucket":      cloudtrail_bucket,
                "github_repo":            github_repo,
                "github_token_secret_arn": github_token_secret_arn,
            },
            ConditionExpression="attribute_not_exists(tenant_id)",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            err = f"Tenant {tenant_id} is already onboarded."
        else:
            err = str(e)
        body = f'<div class="flash err">{err}</div><a href="/register" class="btn btn-secondary">Go back</a>'
        return _page("Error", body)

    cfn_yaml = _cfn_template(tenant_id, external_id, cloudtrail_bucket)

    body = f"""
      <div class="flash ok">
        Tenant <strong>{tenant_id}</strong> added.
        Send the setup template below to your customer — they deploy it once and they're live.
      </div>

      <!-- Steps -->
      <div class="card">
        <h2>What the customer needs to do</h2>
        <div class="step">
          <div class="step-num">1</div>
          <div class="step-body">
            <strong>Deploy the CloudFormation stack below</strong>
            In their AWS console → CloudFormation → Create Stack → paste the template.
            It creates the cross-account IAM role with the correct trust policy and permissions.
          </div>
        </div>
        <div class="step">
          <div class="step-num">2</div>
          <div class="step-body">
            <strong>Make sure CloudTrail is enabled</strong>
            CloudTrail must be logging Management Write events to
            <code>{cloudtrail_bucket}</code> in their account.
            If not already configured, they can enable it in the CloudTrail console.
          </div>
        </div>
        <div class="step">
          <div class="step-num">3</div>
          <div class="step-body">
            <strong>Done</strong>
            The next daily batch run at 7 AM UTC picks them up automatically.
          </div>
        </div>
      </div>

      <!-- CFn template -->
      <div class="card">
        <div class="copy-bar">
          <span>cloudformation-setup.yaml</span>
          <button class="btn btn-secondary" onclick="
            navigator.clipboard.writeText(document.getElementById('cfn').innerText);
            this.textContent='Copied!';
            setTimeout(()=>this.textContent='Copy',1500)
          ">Copy</button>
        </div>
        <pre id="cfn">{cfn_yaml}</pre>
      </div>

      <a href="/" class="btn btn-secondary">&larr; Back to tenants</a>
    """
    return _page("Tenant Added", body)


# ── Delete tenant ─────────────────────────────────────────────────────────────

@app.post("/tenants/{tenant_id}/delete", response_class=HTMLResponse)
async def delete_tenant(tenant_id: str):
    try:
        table.delete_item(Key={"tenant_id": tenant_id})
        return await index(flash=f"Tenant {tenant_id} removed.", flash_type="ok")
    except ClientError as e:
        return await index(flash=str(e), flash_type="err")
