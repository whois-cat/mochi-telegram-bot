output "ecr_repository_name" {
  description = "Name of the ECR repository."
  value       = aws_ecr_repository.app.name
}

output "ecr_repository_url" {
  description = "URL of the ECR repository for docker push."
  value       = aws_ecr_repository.app.repository_url
}

output "ecr_repository_arn" {
  description = "ARN of the ECR repository."
  value       = aws_ecr_repository.app.arn
}

output "lambda_function_name" {
  description = "Name of the Lambda function."
  value       = aws_lambda_function.app.function_name
}

output "lambda_function_url" {
  description = "Public Lambda Function URL."
  value       = aws_lambda_function_url.app.function_url
}

output "lambda_image_uri" {
  description = "ECR image URI used by Lambda."
  value       = aws_lambda_function.app.image_uri
}

output "app_config_secret_name" {
  description = "Secrets Manager secret name for app config."
  value       = aws_secretsmanager_secret.app_config.name
}

output "app_config_secret_arn" {
  description = "Secrets Manager secret ARN for app config."
  value       = aws_secretsmanager_secret.app_config.arn
}

output "known_words_table_name" {
  description = "DynamoDB table name for known words."
  value       = aws_dynamodb_table.known_words.name
}