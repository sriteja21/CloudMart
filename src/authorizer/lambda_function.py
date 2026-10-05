import os
import json
import hashlib
import boto3
import pymysql


def get_db_connection():
    ssm = boto3.client("ssm")

    environment = os.environ.get("ENVIRONMENT", "dev")

    parameter_names = [
        f"/app/{environment}/database/host",
        f"/app/{environment}/database/port",
        f"/app/{environment}/database/name",
        f"/app/{environment}/database/username",
        f"/app/{environment}/database/password"
    ]

    response = ssm.get_parameters(
        Names=parameter_names,
        WithDecryption=True
    )

    parameters = {
        parameter["Name"]: parameter["Value"]
        for parameter in response["Parameters"]
    }

    host = parameters[f"/app/{environment}/database/host"]
    port = int(parameters[f"/app/{environment}/database/port"])
    database = parameters[f"/app/{environment}/database/name"]
    username = parameters[f"/app/{environment}/database/username"]
    password = parameters[f"/app/{environment}/database/password"]

    return pymysql.connect(
        host=host,
        port=port,
        user=username,
        password=password,
        database=database,
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=5,
        read_timeout=5,
        write_timeout=5,
        autocommit=True
    )


def hash_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def extract_customer_id_from_event(event):
    # 1. Check query parameters (?customer_id=X)
    query_params = event.get("queryStringParameters") or {}
    customer_id = query_params.get("customer_id")

    if customer_id and str(customer_id).isdigit():
        return int(customer_id)

    # 2. Check JSON request body
    body = event.get("body")
    if body:
        try:
            if isinstance(body, str):
                body = json.loads(body)

            if isinstance(body, dict):
                customer_id = body.get("customer_id")
                if customer_id and str(customer_id).isdigit():
                    return int(customer_id)
        except Exception:
            pass

    return None


def extract_token_from_event(event):
    headers = event.get("headers") or {}
    authorization_header = (
        headers.get("Authorization") or headers.get("authorization")
    )

    if not authorization_header:
        return None

    parts = authorization_header.strip().split()

    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None

    return parts[1].strip()


def verify_customer_and_token(customer_id, token):
    connection = None

    try:
        token_hash = hash_token(token)
        connection = get_db_connection()

        with connection.cursor() as cursor:
            # Look up by customer_id and confirm token_hash matches
            cursor.execute(
                """
                SELECT customer_id, role
                FROM customers
                WHERE customer_id = %s AND token_hash = %s
                LIMIT 1
                """,
                (customer_id, token_hash)
            )

            customer = cursor.fetchone()

        return customer

    finally:
        if connection:
            connection.close()


def get_request_method(event):
    method = event.get("httpMethod")

    if method:
        return method.upper()

    request_context = event.get("requestContext") or {}
    http = request_context.get("http") or {}

    method = http.get("method")

    if method:
        return method.upper()

    return ""


def get_request_path(event):
    path = event.get("path")

    if path:
        return path

    request_context = event.get("requestContext") or {}

    return request_context.get("http", {}).get("path", "")


def is_path_match(path, allowed_path):
    if path == allowed_path:
        return True

    if allowed_path.endswith("/") and path.startswith(allowed_path):
        return True

    return False


def is_authorized(role, method, path):
    if role == "ADMIN":
        return True

    permissions = {
        "USER": {
            "GET": [
                "/product",
                "/product/",
                "/order",
                "/order/",
                "/order/customer/"
            ],
            "POST": [
                "/order",
                "/order/"
            ],
            "PATCH": [
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
        }
    }

    role_permissions = permissions.get(role)

    if not role_permissions:
        return False

    allowed_paths = role_permissions.get(method, [])

    for allowed_path in allowed_paths:
        if is_path_match(path, allowed_path):
            return True

    return False


def generate_policy(principal_id, effect, event, customer_id=None, role=None):
    method_arn = event.get("methodArn", "*")

    context = {}

    if customer_id is not None:
        context["customer_id"] = str(customer_id)

    if role:
        context["role"] = role

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

    response = {
        "principalId": str(principal_id),
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

    if context:
        response["context"] = context

    return response


def deny(event):
    return generate_policy(
        "unauthorized",
        "Deny",
        event
    )


def lambda_handler(event, context):
    try:
        method = get_request_method(event)
        path = get_request_path(event)

        # Allow public GET requests for product routes without token or customer_id
        if method == "GET" and (path == "/product" or is_path_match(path, "/product/")):
            return generate_policy("anonymous", "Allow", event, role="ANONYMOUS")

        # Extract customer_id from URL query or request body
        customer_id = extract_customer_id_from_event(event)

        if not customer_id:
            return deny(event)

        # Extract Bearer token from authorization header
        token = extract_token_from_event(event)

        if not token:
            return deny(event)

        # Verify customer_id exists and matches the token hash
        customer = verify_customer_and_token(customer_id, token)

        if not customer:
            return deny(event)

        customer_id = customer["customer_id"]
        role = customer["role"]

        if not is_authorized(role, method, path):
            return generate_policy(
                customer_id,
                "Deny",
                event,
                customer_id,
                role
            )

        return generate_policy(
            customer_id,
            "Allow",
            event,
            customer_id,
            role
        )

    except Exception as error:
        print(
            json.dumps(
                {
                    "message": "Authorizer error",
                    "error": str(error)
                }
            )
        )

        return deny(event)