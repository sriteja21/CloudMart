import os
import boto3

ssm = boto3.client("ssm")

ENVIRONMENT = os.environ.get("ENVIRONMENT", "dev")

TOKEN_PARAMETER = f"/app/{ENVIRONMENT}/auth/token"


def get_expected_token():
    """
    Retrieve the authorization token from SSM Parameter Store.
    """

    response = ssm.get_parameter(
        Name=TOKEN_PARAMETER,
        WithDecryption=True
    )

    return response["Parameter"]["Value"]


def lambda_handler(event, context):
    """
    Lambda Authorizer entry point.
    """

    try:
        # Get Authorization header
        headers = event.get("headers") or {}

        authorization_header = (
            headers.get("Authorization")
            or headers.get("authorization")
        )

        if not authorization_header:
            return generate_policy("anonymous", "Deny", event)

        # Expected format:
        # Authorization: Bearer <token>

        parts = authorization_header.split()

        if len(parts) != 2 or parts[0].lower() != "bearer":
            return generate_policy("anonymous", "Deny", event)

        client_token = parts[1]

        # Retrieve expected token from SSM
        expected_token = get_expected_token()

        # Compare tokens
        if client_token == expected_token:
            return generate_policy("cloudmart-user", "Allow", event)

        return generate_policy("anonymous", "Deny", event)

    except Exception as error:

        print(f"Authorizer error: {error}")

        return generate_policy("anonymous", "Deny", event)


def generate_policy(principal_id, effect, event):
    """
    Generate IAM policy returned to API Gateway.
    """

    method_arn = event.get("methodArn", "*")

    return {
        "principalId": principal_id,
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Action": "execute-api:Invoke",
                    "Effect": effect,
                    "Resource": method_arn
                }
            ]
        }
    }