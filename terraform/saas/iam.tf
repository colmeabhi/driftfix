data "aws_caller_identity" "current" {}

# Shared assume-role policy for all Lambda functions
data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

# ── Orchestrator (Processor) Lambda ──────────────────────────────────────────
resource "aws_iam_role" "processor" {
  name               = "${var.project}-processor"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy" "processor" {
  role = aws_iam_role.processor.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "Logs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:*:*:*"
      },
      {
        Sid      = "STSAssume"
        Effect   = "Allow"
        Action   = ["sts:AssumeRole"]
        Resource = "*"
      },
      {
        Sid    = "DynamoDB"
        Effect = "Allow"
        Action = [
          "dynamodb:Scan",
          "dynamodb:BatchGetItem",
          "dynamodb:PutItem",
          "dynamodb:BatchWriteItem"
        ]
        Resource = [aws_dynamodb_table.reconciliations.arn, aws_dynamodb_table.tenants.arn]
      }
    ]
  })
}

# ── Stack Processor Lambda ────────────────────────────────────────────────────
resource "aws_iam_role" "stack_processor" {
  name               = "${var.project}-stack-processor"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy" "stack_processor" {
  role = aws_iam_role.stack_processor.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "Logs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:*:*:*"
      },
      {
        Sid      = "STSAssume"
        Effect   = "Allow"
        Action   = ["sts:AssumeRole"]
        Resource = "*"
      },
      {
        Sid      = "Bedrock"
        Effect   = "Allow"
        Action   = ["bedrock:InvokeModel"]
        Resource = "*"
      },
      {
        Sid      = "DynamoDB"
        Effect   = "Allow"
        Action   = ["dynamodb:UpdateItem"]
        Resource = aws_dynamodb_table.reconciliations.arn
      }
    ]
  })
}

# ── Validator Lambda ──────────────────────────────────────────────────────────
resource "aws_iam_role" "validator" {
  name               = "${var.project}-validator"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy" "validator" {
  role = aws_iam_role.validator.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "Logs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:*:*:*"
      },
      {
        Sid      = "S3Audit"
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:GetObject"]
        Resource = "${aws_s3_bucket.audit.arn}/*"
      }
    ]
  })
}

# ── PR Creator Lambda ─────────────────────────────────────────────────────────
resource "aws_iam_role" "pr_creator" {
  name               = "${var.project}-pr-creator"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy" "pr_creator" {
  role = aws_iam_role.pr_creator.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "Logs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:*:*:*"
      },
      {
        Sid      = "SecretsManager"
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = "arn:aws:secretsmanager:*:${data.aws_caller_identity.current.account_id}:secret:${var.project}/*"
      },
      {
        Sid      = "DynamoDB"
        Effect   = "Allow"
        Action   = ["dynamodb:UpdateItem"]
        Resource = aws_dynamodb_table.reconciliations.arn
      },
      {
        Sid      = "S3Audit"
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "${aws_s3_bucket.audit.arn}/*"
      }
    ]
  })
}
