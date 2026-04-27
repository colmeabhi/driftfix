# Scheduled rule: trigger Processor Lambda once daily at 7 AM UTC
resource "aws_cloudwatch_event_rule" "batch_schedule" {
  name                = "${var.project}-batch-schedule"
  description         = "Trigger drift detection batch processing daily at 7 AM UTC"
  schedule_expression = "cron(0 7 * * ? *)"
}

resource "aws_cloudwatch_event_target" "processor_lambda" {
  rule = aws_cloudwatch_event_rule.batch_schedule.name
  arn  = aws_lambda_function.processor.arn
}

resource "aws_lambda_permission" "eventbridge_invoke_processor" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.processor.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.batch_schedule.arn
}
