import os
import boto3

ssm = boto3.client("ssm")

ENVIRONMENT = os.environ.get("ENVIRONMENT", "dev")

TOKEN_PARAMETERS = {
    "USER": f"/app/{ENVIRONMENT}/auth/user-token",
    "PRODUCT_OWNER": f"/app/{ENVIRONMENT}/auth/product-owner-token",
    "ADMIN": f"/app/{ENVIRONMENT}/auth/admin-token"
}

PERMISSIONS = {
    "USER": {
        "GET": [
            "/customer",
            "/customer/",
            "/order",
            "/order/",
            "/order/customer/"
        ],
        "POST": [
            "/order",
            "/order/"
        ]
    },
    "PRODUCT_OWNER": {
        "GET": [
            "/product",
            "/product/",
            "/order",
            "/order/",
            "/order/customer/"
        ],
        "POST": [
            "/product"
        ],
        "PUT": [
            "/product/"
        ],
        "DELETE": [
            "/product/"
        ]
    },
    "ADMIN": {
        "*": ["*"]
    }
}


def get_tokens():
    response = ssm.get_parameters(
        Names=list(TOKEN_PARAMETERS.values()),
        WithDecryption=True
    )

    tokens = {}

    for parameter in response["Parameters"]:
        for role, parameter_name in TOKEN_PARAMETERS.items():
            if parameter["Name"] == parameter_name:
                tokens[parameter["Value"]] = role

    return tokens


def get_role(token):
    tokens = get_tokens()
    return tokens.get(token)


def is_authorized(role, method, path):
    if role == "ADMIN":
        return True

    role_permissions = PERMISSIONS.get(role, {})
    allowed_paths = role_permissions.get(method, [])

    for allowed_path in allowed_paths:
        if path == allowed_path:
            return True

        if allowed_path.endswith("/") and path.startswith(allowed_path):
            return True

    return False


def lambda_handler(event, context):
    try:
        headers = event.get("headers") or {}

        authorization_header = (
            headers.get("Authorization")
            or headers.get("authorization")
        )

        if not authorization_header:
            return generate_policy(
                "unauthorized",
                "Deny",
                event
            )

        parts = authorization_header.strip().split()

        if len(parts) != 2:
            return generate_policy(
                "unauthorized",
                "Deny",
                event
            )

        if parts[0].lower() != "bearer":
            return generate_policy(
                "unauthorized",
                "Deny",
                event
            )

        client_token = parts[1]

        role = get_role(client_token)

        if not role:
            return generate_policy(
                "unauthorized",
                "Deny",
                event
            )

        method = event.get("httpMethod", "").upper()
        path = event.get("path", "")

        if is_authorized(role, method, path):
            return generate_policy(
                role.lower(),
                "Allow",
                event
            )

        return generate_policy(
            role.lower(),
            "Deny",
            event
        )

    except Exception as error:
        print(f"Authorizer error: {error}")

        return generate_policy(
            "unauthorized",
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