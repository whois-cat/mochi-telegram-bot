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