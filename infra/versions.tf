terraform {
  required_version = ">= 1.6.0"

  backend "s3" {
    bucket       = "mochi-telegram-bot-tfstate-441888286015"
    key          = "mochi-telegram-bot/dev/terraform.tfstate"
    region       = "us-east-1"
    use_lockfile = true
  }

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }

    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}