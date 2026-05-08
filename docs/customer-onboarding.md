# Customer Onboarding Guide

This guide walks you through connecting your AWS account to the Drift Detector SaaS.

There are two ways to onboard — choose the one that fits your situation:

| Method | Best for | Time |
|--------|----------|------|
| **[Self-Service Portal](#method-1-self-service-portal-recommended)** | Most customers — no Terraform or CLI required | ~5 minutes |
| **[Manual (Terraform)](#method-2-manual-terraform)** | Advanced users who already manage infra with Terraform | ~15 minutes |

---

## Method 1 — Self-Service Portal (Recommended)

No Terraform, no CLI, no values to copy-paste. The portal guides you through everything in three steps.

### Step 1 — Open the portal

Go to:

**https://fxcbu33sqc.us-east-1.awsapprunner.com/portal/**

Click **"Connect your AWS account"**.

### Step 2 — Enter your AWS Account ID

Enter your 12-digit AWS account ID. You can find this in the top-right corner of the AWS console, or by running:

```bash
aws sts get-caller-identity --query Account --output text
```

The portal will generate a CloudFormation template specific to your account. Click **"Generate CloudFormation template"**.

### Step 3 — Deploy the CloudFormation template

Download the generated YAML file and deploy it in your AWS account:

1. Open [AWS CloudFormation → Create Stack](https://console.aws.amazon.com/cloudformation/home#/stacks/create/template)
2. Choose **"Upload a template file"** and upload the downloaded YAML
3. Name the stack (e.g. `drift-detector-setup`)
4. On the final review page, check the **IAM capabilities** acknowledgment box
5. Click **"Create stack"** and wait for status **CREATE_COMPLETE** (takes ~1–2 minutes)

The template creates three resources in your account:
- **S3 bucket** — stores your CloudTrail logs (expires after 90 days)
- **CloudTrail trail** — records management write events across all regions
- **Cross-account IAM role** — read-only access so Drift Detector can scan your logs

### Step 4 — Connect GitHub

Return to the portal. Enter:
- **GitHub repository** — the repo where your CloudFormation templates live (format: `owner/repo`)
- **GitHub Personal Access Token** — a fine-grained PAT with **Contents** and **Pull requests** read/write access

[Create a PAT on GitHub →](https://github.com/settings/tokens?type=beta)

Click **"Complete Setup"**. The portal will verify the cross-account role is accessible and activate your account.

### Step 5 — Done

Your account is now connected. Drift Detector runs daily at **7 AM UTC**. When drift is detected, a pull request is automatically opened in your GitHub repository with the reconciliation fix.

---

## Method 2 — Manual (Terraform)

Use this method if you prefer to manage the onboarding resources as Terraform code alongside your existing infra.

### Prerequisites

- AWS CLI configured with credentials for your account
- Terraform >= 1.5 installed
- GitHub repository URL where your CloudFormation templates live (e.g. `your-org/infra-repo`)
- The following values provided by your SaaS contact:
  - `saas_account_id` — the SaaS AWS account ID
  - `external_id` — a shared secret string agreed upon during onboarding

### Step 1 — Create a Terraform IAM user

In your AWS account, create an IAM user for Terraform with the following inline policy. This is used only to run the onboarding Terraform — you can delete it afterward.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "CloudTrail",
      "Effect": "Allow",
      "Action": ["cloudtrail:*"],
      "Resource": "*"
    },
    {
      "Sid": "S3",
      "Effect": "Allow",
      "Action": ["s3:*"],
      "Resource": [
        "arn:aws:s3:::drift-detector-*",
        "arn:aws:s3:::drift-detector-*/*"
      ]
    },
    {
      "Sid": "IAM",
      "Effect": "Allow",
      "Action": ["iam:*"],
      "Resource": "arn:aws:iam::*:role/drift-detector-*"
    },
    {
      "Sid": "CallerIdentity",
      "Effect": "Allow",
      "Action": ["sts:GetCallerIdentity"],
      "Resource": "*"
    }
  ]
}
```

### Step 2 — Create your tfvars file

Inside `terraform/customer/`, create a file named `terraform.tfvars` (this file is gitignored — do not commit it):

```hcl
aws_region      = "us-east-1"
project         = "drift-detector"
saas_account_id = "<provided by SaaS contact>"
external_id     = "<provided by SaaS contact>"
```

### Step 3 — Run Terraform

```bash
cd terraform/customer
terraform init
terraform apply
```

Review the plan and type `yes` to confirm. The apply takes about 2 minutes.

### Step 4 — Send the outputs back

After apply completes, Terraform prints three output values. Send all three to your SaaS contact:

```bash
terraform output
```

| Output | Description |
|--------|-------------|
| `cross_account_role_arn` | The IAM role ARN the SaaS platform will assume |
| `external_id` | Confirms the shared secret (should match what was agreed) |
| `cloudtrail_bucket_name` | The S3 bucket where your CloudTrail logs are stored |

Also provide your **GitHub repository** (e.g. `your-org/infra-repo`) and a **GitHub PAT** with Contents + Pull requests access.

Your SaaS contact will complete the registration and confirm when your account is active.

---

## What Gets Deployed in Your Account

Both methods create the same three resources. We follow a minimal-footprint principle — we do not install agents, modify existing resources, or retain persistent access beyond what is described here.

### 1. CloudTrail Trail

A multi-region trail named `drift-detector-trail` that logs all AWS management write events.

**Why:** CloudTrail is the source of truth for what changed in your account. When an engineer makes a manual change (e.g. modifying an S3 bucket's encryption settings), CloudTrail records the API call. This is how drift is detected.

If you already have CloudTrail enabled, contact your SaaS contact to configure the existing trail instead.

### 2. S3 Bucket for CloudTrail Logs

Named `drift-detector-cloudtrail-<your-account-id>`. Log files are stored in your account — the SaaS platform reads them once daily using the cross-account role. It has no write access.

Log files expire after 90 days by default (configurable).

### 3. Cross-Account IAM Role

Named `drift-detector-cross-account`. Grants the SaaS platform read-only access using your `external_id` as a confused-deputy guard — only our platform that knows your `external_id` can assume the role.

| Permission | Resource | Purpose |
|------------|----------|---------|
| `cloudformation:GetTemplate`, `DescribeStacks`, `ListStacks` | All stacks | Fetch templates for stacks where drift was detected |
| `s3:GetObject`, `s3:ListBucket` | CloudTrail bucket only | Read daily log files |
| `ec2:DescribeTags`, `s3:GetBucketTagging`, `rds:ListTagsForResource`, etc. | All resources (read-only) | Identify which CloudFormation stack a changed resource belongs to |

**What the role cannot do:** write to any resource, create or delete anything, access data outside CloudFormation templates and CloudTrail logs, or assume any other role in your account.

---

## Data Privacy

- **CloudTrail log files** contain a record of API calls (caller identity, timestamp, request parameters). They do not contain the payload of data operations (e.g. the contents of S3 objects).
- **CloudFormation templates** are read once per affected stack per daily batch run and stored in the SaaS audit bucket for 90 days for audit and retry purposes.
- No data is shared with third parties. The SaaS platform runs entirely within AWS infrastructure.

---

## Offboarding

### Self-service portal customers

Contact support to remove your tenant record. Then delete the CloudFormation stack from your AWS console:

1. Go to **AWS CloudFormation → Stacks**
2. Select `drift-detector-setup`
3. Click **Delete**

### Terraform customers

```bash
cd terraform/customer
terraform destroy
```

This deletes the CloudTrail trail, S3 bucket, and IAM role. Contact your SaaS contact to remove your tenant record from the platform.
