output "cross_account_role_arn" {
  description = "Provide this ARN to the SaaS control plane during onboarding"
  value       = aws_iam_role.cross_account.arn
}

output "external_id" {
  description = "Provide this external_id to the SaaS control plane during onboarding"
  value       = var.external_id
  sensitive   = true
}

output "cloudtrail_bucket_name" {
  value = aws_s3_bucket.cloudtrail.bucket
}
