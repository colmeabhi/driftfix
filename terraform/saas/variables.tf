variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "environment" {
  type    = string
  default = "dev"
}

variable "project" {
  type    = string
  default = "drift-detector"
}

# List of customer tenants onboarded to this SaaS platform.
# Each entry maps a tenant_id to the cross-account role ARN and external_id
# the customer created during onboarding.
variable "tenants" {
  type = map(object({
    role_arn    = string
    external_id = string
    github_repo = string # "owner/repo"
  }))
  default = {}
}

variable "github_token_secret_arn" {
  description = "ARN of the Secrets Manager secret holding the GitHub personal access token"
  type        = string
  default     = ""
}

variable "allowed_customer_account_ids" {
  description = "AWS account IDs of onboarded customers allowed to put events onto the SaaS EventBridge bus"
  type        = list(string)
  default     = []
}

variable "bedrock_model_id" {
  description = "Bedrock model ID used for template generation (leave empty until decided)"
  type        = string
  default     = ""
}

variable "lambda_zip_bucket" {
  description = "S3 bucket where Lambda deployment packages are uploaded by scripts/package_lambdas.sh"
  type        = string
}
