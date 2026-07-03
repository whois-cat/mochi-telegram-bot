locals {
  common_tags = {
    Project     = var.project_name
    Environment = var.environment
    ManagedBy   = "terraform"
  }
}

resource "aws_ecr_repository" "app" {
  name = var.project_name

  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }

  force_delete = false

  tags = local.common_tags
}

resource "aws_ecr_lifecycle_policy" "app" {
  repository = aws_ecr_repository.app.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Remove untagged images after 7 days"

        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 7
        }

        action = {
          type = "expire"
        }
      },
      {
        rulePriority = 2
        description  = "Keep only the last N tagged images"

        selection = {
          tagStatus     = "tagged"
          tagPrefixList = ["v"]
          countType     = "imageCountMoreThan"
          countNumber   = var.image_keep_count
        }

        action = {
          type = "expire"
        }
      }
    ]
  })
}

resource "aws_iam_role" "lambda_execution" {
  name = "${var.project_name}-${var.environment}-lambda-execution-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "lambda.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })

  tags = local.common_tags
}

resource "aws_iam_role_policy_attachment" "lambda_basic_execution" {
  role       = aws_iam_role.lambda_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${var.project_name}-${var.environment}"
  retention_in_days = 14

  tags = local.common_tags
}

resource "aws_lambda_function" "app" {
  function_name = "${var.project_name}-${var.environment}"
  role          = aws_iam_role.lambda_execution.arn

  package_type = "Image"
  image_uri    = "${aws_ecr_repository.app.repository_url}:${var.image_tag}"

  architectures = [var.lambda_architecture]

  timeout     = 30
  memory_size = 512

  depends_on = [
    aws_iam_role_policy_attachment.lambda_basic_execution,
    aws_iam_role_policy.lambda_read_app_config_secret,
    aws_iam_role_policy.lambda_known_words_table,
    aws_iam_role_policy.lambda_practice_sessions_table,
    aws_cloudwatch_log_group.lambda
  ]

  tags = local.common_tags
  environment {
    variables = {
      APP_CONFIG_SECRET_ID         = aws_secretsmanager_secret.app_config.name
      KNOWN_WORDS_TABLE_NAME       = aws_dynamodb_table.known_words.name
      PRACTICE_SESSIONS_TABLE_NAME = aws_dynamodb_table.practice_sessions.name
    }
  }
}

resource "aws_lambda_function_url" "app" {
  function_name      = aws_lambda_function.app.function_name
  authorization_type = "NONE"
}

resource "aws_secretsmanager_secret" "app_config" {
  name                    = "${var.project_name}/${var.environment}/app-config"
  description             = "Application secrets for ${var.project_name} ${var.environment}"
  recovery_window_in_days = 7

  tags = local.common_tags
}

resource "aws_iam_role_policy" "lambda_read_app_config_secret" {
  name = "${var.project_name}-${var.environment}-read-app-config-secret"
  role = aws_iam_role.lambda_execution.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "secretsmanager:GetSecretValue"
        ]
        Resource = aws_secretsmanager_secret.app_config.arn
      }
    ]
  })
}

resource "aws_dynamodb_table" "known_words" {
  name         = "${var.project_name}-${var.environment}-known-words"
  billing_mode = "PAY_PER_REQUEST"

  hash_key = "word_key"

  attribute {
    name = "word_key"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true
  }

  deletion_protection_enabled = true

  tags = local.common_tags
}

resource "aws_iam_role_policy" "lambda_known_words_table" {
  name = "${var.project_name}-${var.environment}-known-words-table"
  role = aws_iam_role.lambda_execution.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:Scan"
        ]
        Resource = aws_dynamodb_table.known_words.arn
      }
    ]
  })
}

resource "aws_dynamodb_table" "practice_sessions" {
  name         = "${var.project_name}-${var.environment}-practice-sessions"
  billing_mode = "PAY_PER_REQUEST"

  hash_key = "telegram_user_id"

  attribute {
    name = "telegram_user_id"
    type = "S"
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  server_side_encryption {
    enabled = true
  }

  tags = local.common_tags
}

resource "aws_iam_role_policy" "lambda_practice_sessions_table" {
  name = "${var.project_name}-${var.environment}-practice-sessions-table"
  role = aws_iam_role.lambda_execution.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:DeleteItem"
        ]
        Resource = aws_dynamodb_table.practice_sessions.arn
      }
    ]
  })
}