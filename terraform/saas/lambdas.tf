resource "aws_s3_bucket" "audit" {
  bucket = "${var.project}-audit-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket_versioning" "audit" {
  bucket = aws_s3_bucket.audit.id
  versioning_configuration {
    status = "Enabled"
  }
}

# ── Processor Lambda ─────────────────────────────────────────────────────────
resource "aws_lambda_function" "processor" {
  function_name = "${var.project}-processor"
  role          = aws_iam_role.processor.arn
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 300
  memory_size   = 512

  s3_bucket = var.lambda_zip_bucket
  s3_key    = "processor.zip"

  environment {
    variables = {
      DYNAMODB_TENANTS_TABLE         = aws_dynamodb_table.tenants.name
      DYNAMODB_RECONCILIATIONS_TABLE = aws_dynamodb_table.reconciliations.name
      AUDIT_BUCKET                   = aws_s3_bucket.audit.bucket
      BEDROCK_MODEL_ID               = var.bedrock_model_id
      VALIDATOR_FUNCTION_NAME        = aws_lambda_function.validator.function_name
    }
  }
}

resource "aws_lambda_event_source_mapping" "sqs_to_processor" {
  event_source_arn                   = aws_sqs_queue.drift_events.arn
  function_name                      = aws_lambda_function.processor.arn
  batch_size                         = 10
  function_response_types            = ["ReportBatchItemFailures"]
}

# ── Validator Lambda ──────────────────────────────────────────────────────────
resource "aws_lambda_function" "validator" {
  function_name = "${var.project}-validator"
  role          = aws_iam_role.validator.arn
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 120
  memory_size   = 256

  s3_bucket = var.lambda_zip_bucket
  s3_key    = "validator.zip"

  environment {
    variables = {
      AUDIT_BUCKET            = aws_s3_bucket.audit.bucket
      MAX_RETRIES             = "3"
      PR_CREATOR_FUNCTION_NAME = aws_lambda_function.pr_creator.function_name
    }
  }
}

# ── PR Creator Lambda ─────────────────────────────────────────────────────────
resource "aws_lambda_function" "pr_creator" {
  function_name = "${var.project}-pr-creator"
  role          = aws_iam_role.pr_creator.arn
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 60
  memory_size   = 256

  s3_bucket = var.lambda_zip_bucket
  s3_key    = "pr_creator.zip"

  environment {
    variables = {
      GITHUB_TOKEN_SECRET_ARN        = var.github_token_secret_arn
      DYNAMODB_RECONCILIATIONS_TABLE = aws_dynamodb_table.reconciliations.name
      AUDIT_BUCKET                   = aws_s3_bucket.audit.bucket
    }
  }
}

# Allow Processor Lambda to invoke Validator and PR Creator
resource "aws_iam_role_policy" "processor_invoke_lambdas" {
  role = aws_iam_role.processor.id
  name = "invoke-lambdas"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["lambda:InvokeFunction"]
        Resource = [
          aws_lambda_function.validator.arn,
          aws_lambda_function.pr_creator.arn,
        ]
      }
    ]
  })
}

resource "aws_iam_role_policy" "validator_invoke_pr_creator" {
  role = aws_iam_role.validator.id
  name = "invoke-pr-creator"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["lambda:InvokeFunction"]
        Resource = aws_lambda_function.pr_creator.arn
      }
    ]
  })
}
