# Customer Onboarding Guide

This guide walks you through connecting your AWS account to the Drift Detector SaaS. The setup takes about 15 minutes and requires Terraform and AWS CLI access to your account.

---

## What You Need Before Starting

- AWS CLI configured with credentials for your account
- Terraform >= 1.5 installed
- Your GitHub repository URL where your CloudFormation templates live (e.g. `your-org/infra-repo`)
- The following values provided by your SaaS contact:
  - `saas_account_id` — the SaaS AWS account ID
  - `external_id` — a shared secret string agreed upon during onboarding

---

## Section 1 — Setup Instructions

### Step 1 — Create a Terraform IAM user

In your AWS account, create an IAM user for Terraform with the following inline policy. This is used only to run the onboarding Terraform — you can delete it afterward if you prefer.

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

After apply completes, Terraform prints three output values. Send all three to your SaaS contact — they are needed to complete your onboarding:

```bash
terraform output
```

| Output | Description |
|--------|-------------|
| `cross_account_role_arn` | The IAM role ARN the SaaS platform will assume |
| `external_id` | Confirms the shared secret (should match what was agreed) |
| `cloudtrail_bucket_name` | The S3 bucket where your CloudTrail logs are stored |

Also provide your **GitHub repository** (e.g. `your-org/infra-repo`) where your CloudFormation templates live.

That's it — your SaaS contact will complete the registration and confirm when your account is active.

---

## Section 2 — What We Deploy in Your Account

We follow a minimal-footprint principle: we create exactly three resources in your account, all scoped tightly to what is needed. We do not install agents, modify existing resources, or retain persistent access beyond what is described here.

### 1. CloudTrail Trail

We enable a CloudTrail trail that logs all AWS API calls across all regions. If you already have CloudTrail enabled, you can skip this resource — speak to your SaaS contact to configure the existing trail instead.

**Why:** CloudTrail is the source of truth for what changed in your account. When an engineer makes a manual change in the AWS console (e.g. modifying an S3 bucket's encryption settings), CloudTrail records the API call. This is how drift is detected.

**What it creates:**
- A multi-region trail named `drift-detector-trail`
- An S3 bucket named `drift-detector-cloudtrail-<your-account-id>` to store the log files
- A bucket policy allowing only the CloudTrail service to write to it

### 2. S3 Bucket for CloudTrail Logs

Log files are stored in your account, in a bucket we create. The SaaS platform reads these logs once daily using the cross-account role described below — it does not have any write access to this bucket.

**Retention:** Log files are retained indefinitely by default. You can add a lifecycle rule to expire them after your desired retention period.

### 3. Cross-Account IAM Role

This is the trust boundary between your account and the SaaS platform. The role grants the SaaS account permission to assume it, using your `external_id` as a guard against confused deputy attacks — meaning only the SaaS platform that knows your `external_id` can assume the role.

**What the role can do:**

| Permission | Resource | Purpose |
|------------|----------|---------|
| `cloudformation:GetTemplate` | All stacks | Fetch the current CloudFormation template for a stack where drift was detected |
| `cloudformation:DescribeStacks` | All stacks | Read stack metadata |
| `cloudformation:ListStacks` | All stacks | Enumerate stacks |
| `s3:GetObject`, `s3:ListBucket`, `s3:ListObjectsV2` | CloudTrail S3 bucket only | Read the daily log files |
| `ec2:DescribeTags`, `s3:GetBucketTagging`, `rds:ListTagsForResource`, etc. | All resources (read-only) | Look up the `aws:cloudformation:stack-name` tag on a changed resource to identify which stack it belongs to |

**What the role cannot do:** write to any resource, create or delete anything, access any data outside of CloudFormation templates and CloudTrail logs, or assume any other role in your account.

The `external_id` is a randomly generated string agreed upon during onboarding. Without it, the SaaS platform cannot assume the role even if it knows the role ARN.

---

## Section 3 — Data Privacy

- **CloudTrail log files** contain a record of API calls made in your account including the caller identity, timestamp, and request parameters. They do not contain the payload of data operations (e.g. the contents of S3 objects).
- **CloudFormation templates** are read once per affected stack per daily batch run. They are stored in the SaaS audit bucket for 90 days for audit and retry purposes.
- No data is shared with third parties. The SaaS platform runs entirely within AWS infrastructure.

---

## Section 4 — Offboarding

To remove the integration, run:

```bash
cd terraform/customer
terraform destroy
```

This deletes the CloudTrail trail, S3 bucket, and IAM role. The SaaS platform will no longer be able to access your account. Contact your SaaS contact to remove your tenant record from the platform.
