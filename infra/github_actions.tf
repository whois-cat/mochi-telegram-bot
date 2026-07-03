locals {
  github_owner  = "whois-cat"
  github_repo   = "mochi-telegram-bot"
  github_branch = "main"

  github_repo_full_name = "${local.github_owner}/${local.github_repo}"
  github_oidc_subject   = "repo:${local.github_repo_full_name}:ref:refs/heads/${local.github_branch}"
}

data "aws_caller_identity" "current" {}

data "tls_certificate" "github_actions" {
  url = "https://token.actions.githubusercontent.com"
}

resource "aws_iam_openid_connect_provider" "github_actions" {
  url = "https://token.actions.githubusercontent.com"

  client_id_list = [
    "sts.amazonaws.com"
  ]

  thumbprint_list = [
    data.tls_certificate.github_actions.certificates[0].sha1_fingerprint
  ]

  tags = local.common_tags
}

resource "aws_iam_role" "github_actions_deploy" {
  name = "${var.project_name}-${var.environment}-github-actions-deploy"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Federated = aws_iam_openid_connect_provider.github_actions.arn
        }
        Action = "sts:AssumeRoleWithWebIdentity"
        Condition = {
          StringEquals = {
            "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
            "token.actions.githubusercontent.com:sub" = local.github_oidc_subject
          }
        }
      }
    ]
  })

  tags = local.common_tags
}

# Bootstrap permission for the first working CI/CD pipeline.
# Later we should replace this with a narrower project-scoped policy.
resource "aws_iam_role_policy_attachment" "github_actions_admin_bootstrap" {
  role       = aws_iam_role.github_actions_deploy.name
  policy_arn = "arn:aws:iam::aws:policy/AdministratorAccess"
}
