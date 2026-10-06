import os
import json
import hashlib
import logging
import re
import time

import boto3
import pymysql

from botocore.exceptions import BotoCoreError, ClientError


# ============================================================
# LOGGING CONFIGURATION
# ============================================================

logger = logging.getLogger()
logger.setLevel(logging.INFO)


# ============================================================
# DATABASE CONNECTION
# ============================================================

def get_db_connection():
    """
    Get database credentials from SSM Parameter Store
    and establish a MySQL connection.
    """

    logger.info("Starting database connection")

    ssm = boto3.client("ssm")

    environment = os.environ.get("ENVIRONMENT", "dev")

    logger.info(
        "Fetching database parameters from SSM. environment=%s",
        environment
    )

    parameter_names = [
        f"/app/{environment}/database/host",
        f"/app/{environment}/database/port",
        f"/app/{environment}/database/name",
        f"/app/{environment}/database/username",
        f"/app/{environment}/database/password"
    ]

    try:
        response = ssm.get_parameters(
            Names=parameter_names,
            WithDecryption=True
        )

        logger.info(
            "SSM database parameters retrieved. requested=%d returned=%d",
            len(parameter_names),
            len(response.get("Parameters", []))
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

        logger.info(
            "Database configuration loaded. host=%s port=%s database=%s",
            host,
            port,
            database
        )

        connection = pymysql.connect(
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

        logger.info("Database connection established successfully")

        return connection

    except Exception:
        logger.exception("Failed to establish database connection")
        raise


# ============================================================
# TOKEN HASHING
# ============================================================

def hash_token(token):
    """
    Hash the supplied token.

    IMPORTANT:
    The actual token and hash are never written to CloudWatch logs.
    """

    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ============================================================
# CUSTOMER ID EXTRACTION
# ============================================================

def extract_customer_id_from_event(event):
    """
    Extract customer_id from:
    1. Query string        ?customer_id=7
    2. X-Customer-Id header

    NOTE: a REQUEST authorizer never receives the request body, so the
    caller must send the id in the query string or the header. The backend
    Lambda must still check that any customer_id inside the JSON body
    matches the authorizer context (requestContext.authorizer.customer_id).
    """

    logger.info("Attempting to extract customer_id")

    query_params = event.get("queryStringParameters") or {}

    headers = {
        str(k).lower(): v
        for k, v in (event.get("headers") or {}).items()
    }

    raw = query_params.get("customer_id") or headers.get("x-customer-id")

    if raw and str(raw).isdigit():

        customer_id = int(raw)

        logger.info(
            "customer_id extracted. source=%s customer_id=%s",
            "query" if query_params.get("customer_id") else "header",
            customer_id
        )

        return customer_id

    logger.warning(
        "Unable to extract a valid customer_id from request"
    )

    return None


# ============================================================
# TOKEN EXTRACTION
# ============================================================

def extract_token_from_event(event):
    """
    Extract Bearer token from Authorization header.

    The actual token is NEVER logged.
    """

    logger.info("Attempting to extract Authorization header")

    headers = event.get("headers") or {}

    authorization_header = (
        headers.get("Authorization")
        or headers.get("authorization")
    )

    if not authorization_header:

        logger.warning(
            "Authorization header is missing"
        )

        return None

    logger.info(
        "Authorization header is present"
    )

    parts = authorization_header.strip().split()

    if len(parts) != 2:

        logger.warning(
            "Invalid Authorization header format"
        )

        return None

    if parts[0].lower() != "bearer":

        logger.warning(
            "Authorization header does not use Bearer scheme"
        )

        return None

    token = parts[1].strip()

    if not token:

        logger.warning(
            "Bearer token is empty"
        )

        return None

    logger.info(
        "Bearer token successfully extracted"
    )

    return token


# ============================================================
# CUSTOMER + TOKEN VERIFICATION
# ============================================================

def verify_customer_and_token(customer_id, token):

    connection = None

    logger.info(
        "Starting customer and token verification. customer_id=%s",
        customer_id
    )

    try:

        token_hash = hash_token(token)

        logger.info(
            "Token hash generated for verification"
        )

        connection = get_db_connection()

        with connection.cursor() as cursor:

            logger.info(
                "Looking up customer and token hash. customer_id=%s",
                customer_id
            )

            cursor.execute(
                """
                SELECT customer_id, role
                FROM customers
                WHERE customer_id = %s
                  AND token_hash = %s
                LIMIT 1
                """,
                (customer_id, token_hash)
            )

            customer = cursor.fetchone()

        if customer:

            logger.info(
                "Customer authentication successful. customer_id=%s role=%s",
                customer["customer_id"],
                customer["role"]
            )

        else:

            logger.warning(
                "Customer authentication failed. "
                "No matching customer_id/token combination. customer_id=%s",
                customer_id
            )

        return customer

    except Exception:

        logger.exception(
            "Error while verifying customer and token. customer_id=%s",
            customer_id
        )

        raise

    finally:

        if connection:

            try:
                connection.close()

                logger.info(
                    "Database connection closed"
                )

            except Exception:

                logger.exception(
                    "Failed to close database connection"
                )


# ============================================================
# HTTP METHOD
# ============================================================

def get_request_method(event):

    method = event.get("httpMethod")

    if method:

        method = method.upper()

        logger.info(
            "HTTP method extracted from httpMethod=%s",
            method
        )

        return method

    request_context = event.get("requestContext") or {}

    http = request_context.get("http") or {}

    method = http.get("method")

    if method:

        method = method.upper()

        logger.info(
            "HTTP method extracted from requestContext=%s",
            method
        )

        return method

    logger.warning(
        "Unable to determine HTTP method"
    )

    return ""


# ============================================================
# REQUEST PATH
# ============================================================

def get_request_path(event):

    path = event.get("path")

    if path:

        logger.info(
            "Request path extracted from event.path=%s",
            path
        )

        return path

    request_context = event.get("requestContext") or {}

    path = request_context.get("http", {}).get("path", "")

    logger.info(
        "Request path extracted from requestContext=%s",
        path
    )

    return path


# ============================================================
# PATH MATCHING
# ============================================================

def is_path_match(path, allowed_path):

    if path == allowed_path:
        return True

    if allowed_path.endswith("/") and path.startswith(allowed_path):
        return True

    return False


# ============================================================
# ROLE AUTHORIZATION
# ============================================================

def is_authorized(role, method, path):

    logger.info(
        "Checking role authorization. role=%s method=%s path=%s",
        role,
        method,
        path
    )

    # --------------------------------------------------------
    # ADMIN
    # --------------------------------------------------------

    if role == "ADMIN":

        logger.info(
            "Authorization allowed because role=ADMIN"
        )

        return True

    # --------------------------------------------------------
    # USER / PRODUCT OWNER PERMISSIONS
    # --------------------------------------------------------

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

        logger.warning(
            "Authorization denied. Unknown or unsupported role=%s",
            role
        )

        return False

    allowed_paths = role_permissions.get(method, [])

    logger.info(
        "Allowed paths for role=%s method=%s: %s",
        role,
        method,
        allowed_paths
    )

    for allowed_path in allowed_paths:

        if is_path_match(path, allowed_path):

            logger.info(
                "Authorization allowed. role=%s method=%s path=%s matched=%s",
                role,
                method,
                path,
                allowed_path
            )

            return True

    logger.warning(
        "Authorization denied. "
        "No permission match. role=%s method=%s path=%s",
        role,
        method,
        path
    )

    return False


# ============================================================
# POLICY GENERATION
# ============================================================

def generate_policy(
    principal_id,
    effect,
    event,
    customer_id=None,
    role=None
):

    method_arn = event.get("methodArn", "*")

    context = {}

    if customer_id is not None:
        context["customer_id"] = str(customer_id)

    if role:
        context["role"] = role

    # Scope the policy to exactly the method/path being invoked, so a
    # cached or reused result can never allow other routes.
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

    logger.info(
        "Policy generated. effect=%s principal=%s customer_id=%s role=%s",
        effect,
        principal_id,
        customer_id,
        role
    )

    return response


# ============================================================
# DENY POLICY
# ============================================================

def deny(event):

    logger.warning(
        "Generating DENY policy"
    )

    return generate_policy(
        "unauthorized",
        "Deny",
        event
    )


# ============================================================
# MAIN LAMBDA HANDLER
# ============================================================

def lambda_handler(event, context):

    start_time = time.time()

    logger.info(
        "============================================================"
    )

    logger.info(
        "AUTHORIZER INVOCATION STARTED"
    )

    try:

        # ----------------------------------------------------
        # Request information
        # ----------------------------------------------------

        method = get_request_method(event)

        path = get_request_path(event)

        logger.info(
            "Incoming request. method=%s path=%s",
            method,
            path
        )

        # ----------------------------------------------------
        # Public GET /product
        # ----------------------------------------------------

        if (
            method == "GET"
            and (
                path == "/product"
                or is_path_match(path, "/product/")
            )
        ):

            logger.info(
                "Public product GET request detected. "
                "Authentication is not required."
            )

            response = generate_policy(
                "anonymous",
                "Allow",
                event,
                role="ANONYMOUS"
            )

            logger.info(
                "AUTHORIZATION RESULT: ALLOW - ANONYMOUS PRODUCT GET"
            )

            return response

        # ----------------------------------------------------
        # Extract customer ID
        # ----------------------------------------------------

        customer_id = extract_customer_id_from_event(event)

        if not customer_id:

            logger.warning(
                "AUTHORIZATION RESULT: DENY - customer_id missing"
            )

            return deny(event)

        # ----------------------------------------------------
        # Extract token
        # ----------------------------------------------------

        token = extract_token_from_event(event)

        if not token:

            logger.warning(
                "AUTHORIZATION RESULT: DENY - token missing or invalid"
            )

            return deny(event)

        # ----------------------------------------------------
        # Verify customer + token
        # ----------------------------------------------------

        logger.info(
            "Starting authentication. customer_id=%s",
            customer_id
        )

        customer = verify_customer_and_token(
            customer_id,
            token
        )

        if not customer:

            logger.warning(
                "AUTHORIZATION RESULT: DENY - "
                "customer/token verification failed. customer_id=%s",
                customer_id
            )

            return deny(event)

        # ----------------------------------------------------
        # Get authenticated customer information
        # ----------------------------------------------------

        customer_id = customer["customer_id"]
        role = customer["role"]

        logger.info(
            "Authenticated customer. customer_id=%s role=%s",
            customer_id,
            role
        )

        # ----------------------------------------------------
        # Non-admins may only read their own order list
        # ----------------------------------------------------

        own_orders = re.match(r"^/order/customer/(\d+)/?$", path)

        if role != "ADMIN" and own_orders and int(own_orders.group(1)) != int(customer_id):

            logger.warning(
                "AUTHORIZATION RESULT: DENY - customer_id=%s requested "
                "orders of customer %s",
                customer_id,
                own_orders.group(1)
            )

            return generate_policy(
                customer_id,
                "Deny",
                event,
                customer_id,
                role
            )

        # ----------------------------------------------------
        # Role-based authorization
        # ----------------------------------------------------

        authorized = is_authorized(
            role,
            method,
            path
        )

        if not authorized:

            logger.warning(
                "AUTHORIZATION RESULT: DENY. "
                "customer_id=%s role=%s method=%s path=%s",
                customer_id,
                role,
                method,
                path
            )

            return generate_policy(
                customer_id,
                "Deny",
                event,
                customer_id,
                role
            )

        # ----------------------------------------------------
        # Authorization successful
        # ----------------------------------------------------

        logger.info(
            "AUTHORIZATION RESULT: ALLOW. "
            "customer_id=%s role=%s method=%s path=%s",
            customer_id,
            role,
            method,
            path
        )

        return generate_policy(
            customer_id,
            "Allow",
            event,
            customer_id,
            role
        )

    except Exception as error:

        logger.exception(
            "AUTHORIZER ERROR: %s",
            str(error)
        )

        return deny(event)

    finally:

        duration_ms = round(
            (time.time() - start_time) * 1000,
            2
        )

        logger.info(
            "AUTHORIZER INVOCATION COMPLETED. duration_ms=%s",
            duration_ms
        )

        logger.info(
            "============================================================"
        )