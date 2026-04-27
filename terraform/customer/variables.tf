variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "project" {
  type    = string
  default = "drift-detector"
}

# The SaaS account ID that is allowed to assume the cross-account role
variable "saas_account_id" {
  type = string
}

# A random string agreed upon during onboarding to prevent confused deputy attacks
variable "external_id" {
  type = string
}

