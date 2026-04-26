# Cross-account IAM role — SaaS account assumes this to read CFN templates
resource "aws_iam_role" "cross_account" {
  name = "${var.project}-cross-account"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowSaaSAccount"
        Effect = "Allow"
        Principal = {
          AWS = "arn:aws:iam::${var.saas_account_id}:root"
        }
        Action = "sts:AssumeRole"
        Condition = {
          StringEquals = {
            "sts:ExternalId" = var.external_id
          }
        }
      }
    ]
  })
}

resource "aws_iam_role_policy" "cross_account_read" {
  role = aws_iam_role.cross_account.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ReadCloudFormation"
        Effect = "Allow"
        Action = [
          "cloudformation:GetTemplate",
          "cloudformation:DescribeStacks",
          "cloudformation:ListStacks"
        ]
        Resource = "*"
      },
      {
        Sid    = "ReadCloudTrailLogs"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:ListBucket"
        ]
        Resource = [
          aws_s3_bucket.cloudtrail.arn,
          "${aws_s3_bucket.cloudtrail.arn}/*"
        ]
      }
    ]
  })
}
