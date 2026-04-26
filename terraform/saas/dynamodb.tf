resource "aws_dynamodb_table" "reconciliations" {
  name         = "${var.project}-reconciliations"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "tenant_id"
  range_key    = "event_id"

  attribute {
    name = "tenant_id"
    type = "S"
  }

  attribute {
    name = "event_id"
    type = "S"
  }

  attribute {
    name = "status"
    type = "S"
  }

  attribute {
    name = "processed_at"
    type = "S"
  }

  global_secondary_index {
    name            = "status-processed_at-index"
    hash_key        = "status"
    range_key       = "processed_at"
    projection_type = "ALL"
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }
}

resource "aws_dynamodb_table" "tenants" {
  name         = "${var.project}-tenants"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "tenant_id"

  attribute {
    name = "tenant_id"
    type = "S"
  }
}
