# CloudMart GitHub Actions AWS OIDC Deployment Runbook

This runbook describes the AWS and GitHub configuration and deployment process used by CloudMart. GitHub Actions authenticates to AWS using **OIDC and temporary credentials** instead of storing long-term AWS access keys.

---

## 1. Prerequisites

Before starting the deployment, ensure the following are available:

- AWS account with permission to create and manage the required IAM resources.
- GitHub repository containing the CloudMart deployment workflow.
- AWS Region: `ap-south-1`.
- CloudFormation templates and deployment workflow.
- Required GitHub Secrets.

---

# Part A — AWS Configuration

## 2. Create the GitHub OIDC Identity Provider

Open:

**AWS Console → IAM → Identity providers → Add provider**

Configure the provider as follows:

| Setting | Value |
|---|---|
| Provider type | OpenID Connect |
| Provider URL | `https://token.actions.githubusercontent.com` |
| Audience | `sts.amazonaws.com` |

Create the identity provider.

---

## 3. Create the GitHub Actions IAM Role

Create the following IAM role:

```text
CloudMart-GitHubActions-Role
```

This role is assumed by GitHub Actions through OIDC and contains the permissions required to deploy the CloudMart CloudFormation stacks and AWS resources.

The role requires permissions for the services used by CloudMart, including:

- CloudFormation
- S3
- Lambda
- API Gateway
- IAM
- EC2 / VPC
- RDS
- EventBridge
- SNS
- CloudWatch / CloudWatch Logs
- SSM Parameter Store

---

## 4. Configure the IAM Role Trust Policy

Open:

**AWS Console → IAM → Roles → CloudMart-GitHubActions-Role → Trust relationships → Edit trust policy**

Use the following trust policy:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Federated": "arn:aws:iam::${AWS_ACCOUNT_ID}:oidc-provider/token.actions.githubusercontent.com"
      },
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": {
          "token.actions.githubusercontent.com:aud": "sts.amazonaws.com"
        },
        "StringLike": {
          "token.actions.githubusercontent.com:sub": [
            "repo:sriteja21@*/CloudMart@*:ref:refs/heads/main"
          ]
        }
      }
    }
  ]
}
```

Replace:

```text
${AWS_ACCOUNT_ID}
```

with the AWS account ID.

### Trust Policy Requirements

- The OIDC provider must match `token.actions.githubusercontent.com`.
- The audience must be `sts.amazonaws.com`.
- The subject restricts access to the CloudMart repository.
- Only the `main` branch can assume the deployment role.

---

## 5. GitHub → AWS OIDC Flow

The deployment authentication flow is:

```text
git push
    ↓
GitHub Actions
    ↓
GitHub OIDC token
    ↓
AWS STS AssumeRoleWithWebIdentity
    ↓
CloudMart-GitHubActions-Role
    ↓
Temporary AWS credentials
    ↓
CloudFormation / S3 / Lambda / Other AWS Services
```

### Important Requirements

- The workflow requires:

```yaml
permissions:
  id-token: write
```

- The AWS role ARN is supplied to the workflow through GitHub Secrets.
- No long-term AWS access key is required for the deployment.

---

## 6. GitHub Secrets

The following secrets are required:

| Secret | Purpose |
|---|---|
| `AWS_ROLE_ARN` | ARN of `CloudMart-GitHubActions-Role` |
| `DB_PASSWORD` | RDS database password |
| `AUTH_TOKEN` | Admin API and dashboard token |
| `LOW_STOCK_EMAIL` | SNS low-stock notification email |
| `ORDER_NOTIFICATION_EMAIL` | SNS order notification email |

> **Security:** Never commit these values to the repository.

---

## 7. SSM Parameter Store

During deployment, the IAM Stack creates the required CloudMart database parameters in SSM Parameter Store.

The following parameters are used:

```text
/app/<environment>/database/host
/app/<environment>/database/port
/app/<environment>/database/name
/app/<environment>/database/username
/app/<environment>/database/password
/app/<environment>/s3/report-bucket
```

### Configuration Sources

Non-sensitive settings such as the following are read from:

```text
infrastructure/config/parameters.json
```

Examples include:

- Environment
- CIDRs
- Database name
- Database instance class

Sensitive values are passed through GitHub Secrets.

---

# Part B — Deployment

## 8. Start the Deployment

### Step 1 — Make Changes

Make the required code or configuration changes.

### Step 2 — Commit and Push

Add and commit the changes:

```bash
git add .
git commit -m "deployment update"
git push origin main
```

GitHub Actions automatically starts the deployment after the push.

### Step 3 — Open GitHub Actions

Open:

**GitHub → Actions → CloudMart Deploy Pipeline**

---

## 9. Deployment Order

CloudMart stacks are deployed in the following order:

```text
Validate
   ↓
Network
   ↓
Data
   ↓
IAM
   ↓
Auth
   ↓
Event-Driven
   ↓
Application
   ↓
Reporting
   ↓
Monitoring
   ↓
Dashboard
```

Wait until all deployment jobs show **Success**.

---

## 10. Initialize the Database

After the **Auth** job succeeds, run the schema Lambda once:

```bash
aws lambda invoke \
  --function-name cloudmart-dev-initschema \
  --payload '{}' \
  --cli-binary-format raw-in-base64-out \
  --region ap-south-1 \
  out.json
```

Check the generated output:

```bash
cat out.json
```

---

## 11. Deployment Outputs

Copy the following values from the GitHub Actions workflow output:

- **API Invoke URL**
- **Dashboard URL**
- **Operations Dashboard**

The CloudWatch dashboard is available under:

```text
CloudWatch → Dashboards → cloudmart-operations-<environment>
```

---

## 12. Manual Deployment

A deployment can also be started manually.

Navigate to:

**GitHub → Actions → CloudMart Deploy Pipeline → Run workflow**

Select:

```text
Branch: main
```

Then select:

**Run workflow**

