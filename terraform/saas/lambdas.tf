resource "aws_s3_bucket" "audit" {
  bucket = "${var.project}-audit-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket_versioning" "audit" {
  bucket = aws_s3_bucket.audit.id
  versioning_configuration {
    status = "Enabled"
  }
}

# ── Orchestrator (Processor) Lambda ──────────────────────────────────────────
resource "aws_lambda_function" "processor" {
  function_name = "${var.project}-processor"
  role          = aws_iam_role.processor.arn
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 900 # 15 min — reads all tenants' CloudTrail logs and fans out
  memory_size   = 512

  s3_bucket = var.lambda_zip_bucket
  s3_key    = "processor.zip"

  environment {
    variables = {
      DYNAMODB_TENANTS_TABLE         = aws_dynamodb_table.tenants.name
      DYNAMODB_RECONCILIATIONS_TABLE = aws_dynamodb_table.reconciliations.name
      STACK_PROCESSOR_FUNCTION_NAME  = aws_lambda_function.stack_processor.function_name
    }
  }
}

resource "aws_lambda_function_event_invoke_config" "processor_destinations" {
  function_name = aws_lambda_function.processor.function_name

  destination_config {
    on_failure {
      destination = aws_sqs_queue.processor_dlq.arn
    }
  }
}

# ── Stack Processor Lambda ────────────────────────────────────────────────────
resource "aws_lambda_function" "stack_processor" {
  function_name = "${var.project}-stack-processor"
  role          = aws_iam_role.stack_processor.arn
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 300
  memory_size   = 512

  s3_bucket = var.lambda_zip_bucket
  s3_key    = "stack_processor.zip"

  environment {
    variables = {
      DYNAMODB_RECONCILIATIONS_TABLE = aws_dynamodb_table.reconciliations.name
      BEDROCK_MODEL_ID               = var.bedrock_model_id
      VALIDATOR_FUNCTION_NAME        = aws_lambda_function.validator.function_name
    }
  }
}

resource "aws_lambda_function_event_invoke_config" "stack_processor_destinations" {
  function_name = aws_lambda_function.stack_processor.function_name

  destination_config {
    on_failure {
      destination = aws_sqs_queue.processor_dlq.arn
    }
  }
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
      AUDIT_BUCKET             = aws_s3_bucket.audit.bucket
      MAX_RETRIES              = "3"
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

# ── Invoke permissions ────────────────────────────────────────────────────────
resource "aws_iam_role_policy" "processor_invoke_stack_processor" {
  role = aws_iam_role.processor.id
  name = "invoke-stack-processor"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["lambda:InvokeFunction"]
      Resource = aws_lambda_function.stack_processor.arn
    }]
  })
}

resource "aws_iam_role_policy" "stack_processor_invoke_validator" {
  role = aws_iam_role.stack_processor.id
  name = "invoke-validator"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["lambda:InvokeFunction"]
      Resource = aws_lambda_function.validator.arn
    }]
  })
}

resource "aws_iam_role_policy" "validator_invoke_pr_creator" {
  role = aws_iam_role.validator.id
  name = "invoke-pr-creator"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["lambda:InvokeFunction"]
      Resource = aws_lambda_function.pr_creator.arn
    }]
  })
}
