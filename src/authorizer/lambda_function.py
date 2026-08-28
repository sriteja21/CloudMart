import os
import boto3

ssm = boto3.client("ssm")

ENVIRONMENT = os.environ.get("ENVIRONMENT", "dev")
TOKEN_PARAMETER = f"/app/{ENVIRONMENT}/auth/token"


def get_expected_token():
    response = ssm.get_parameter(
        Name=TOKEN_PARAMETER,
        WithDecryption=True
    )

    return response["Parameter"]["Value"]


def lambda_handler(event, context):

    try:
        headers = event.get("headers") or {}

        authorization_header = (
            headers.get("Authorization")
            or headers.get("authorization")
        )

        if not authorization_header:
            return generate_policy(
                "anonymous",
                "Deny",
                event
            )

        parts = authorization_header.strip().split()

        if len(parts) != 2:
            return generate_policy(
                "anonymous",
                "Deny",
                event
            )

        if parts[0].lower() != "bearer":
            return generate_policy(
                "anonymous",
                "Deny",
                event
            )

        client_token = parts[1]
        expected_token = get_expected_token()

        if client_token == expected_token:
            return generate_policy(
                "cloudmart-user",
                "Allow",
                event
            )

        return generate_policy(
            "anonymous",
            "Deny",
            event
        )

    except Exception as error:
        print(f"Authorizer error: {error}")

        return generate_policy(
            "anonymous",
            "Deny",
            event
        )


def generate_policy(principal_id, effect, event):

    method_arn = event.get("methodArn", "*")

    if method_arn == "*":
        resource = "*"

    else:
        arn_parts = method_arn.split("/")

        if len(arn_parts) >= 2:
            resource = (
                arn_parts[0]
                + "/"
                + arn_parts[1]
                + "/*/*"
            )
        else:
            resource = method_arn

    return {
        "principalId": principal_id,
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Action": "execute-api:Invoke",
                    "Effect": effect,
                    "Resource": resource
                }
            ]
        }
    }