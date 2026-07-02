variable "aws_region" {
  description = "AWS region where project resources will be created."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Project name used for AWS resource names."
  type        = string
  default     = "mochi-telegram-bot"

  validation {
    condition     = can(regex("^[a-z0-9-]+$", var.project_name))
    error_message = "project_name must contain only lowercase letters, numbers, and hyphens."
  }
}

variable "environment" {
  description = "Deployment environment."
  type        = string
  default     = "dev"
}

variable "image_keep_count" {
  description = "How many tagged ECR images to keep."
  type        = number
  default     = 10
}

variable "image_tag" {
  description = "ECR image tag used by Lambda."
  type        = string
  default     = "v1"
}

variable "lambda_architecture" {
  description = "Lambda CPU architecture. Must match Docker image platform."
  type        = string
  default     = "x86_64"

  validation {
    condition     = contains(["x86_64", "arm64"], var.lambda_architecture)
    error_message = "lambda_architecture must be x86_64 or arm64."
  }
}