import json
import logging
import os
import re
import hashlib
import secrets
import time
from functools import wraps

import boto3
import pymysql

VALID_ROLES = {"USER", "PRODUCT_OWNER", "ADMIN"}


def log_endpoint(func):
    @wraps(func)
    def wrapper(event, *args, **kwargs):
        start = time.time()
        method = event.get("httpMethod", "").upper()
        path = event.get("path", "")

        logging.getLogger().info(
            "START function=%s method=%s path=%s",
            func.__name__,
            method,
            path
        )

        try:
            result = func(event, *args, **kwargs)

            logging.getLogger().info(
                "END function=%s status=%s duration_ms=%.2f",
                func.__name__,
                result.get("statusCode")
                if isinstance(result, dict)
                else None,
                (time.time() - start) * 1000
            )

            return result

        except Exception:
            logging.getLogger().exception(
                "FAILED function=%s duration_ms=%.2f",
                func.__name__,
                (time.time() - start) * 1000
            )
            raise

    return wrapper


def hash_token(token):
    if not token or not isinstance(token, str):
        raise ValueError(
            "Customer token must be a non-empty string."
        )

    return hashlib.sha256(
        token.encode("utf-8")
    ).hexdigest()


def get_environment():
    return os.getenv("ENVIRONMENT", "dev")


def get_db_connection():
    logger = logging.getLogger()
    environment = get_environment()

    names = [
        f"/app/{environment}/database/host",
        f"/app/{environment}/database/port",
        f"/app/{environment}/database/name",
        f"/app/{environment}/database/username",
        f"/app/{environment}/database/password"
    ]

    ssm = boto3.client("ssm")

    logger.info(
        "Reading database configuration from SSM environment=%s",
        environment
    )

    result = ssm.get_parameters(
        Names=names,
        WithDecryption=True
    )

    parameters = result.get("Parameters", [])

    if len(parameters) != len(names):
        found = {item["Name"] for item in parameters}
        missing = [name for name in names if name not in found]
        raise RuntimeError(
            "Database configuration is incomplete. "
            f"Missing parameters: {', '.join(missing)}"
        )

    values = {
        item["Name"].split("/")[-1]: item["Value"]
        for item in parameters
    }

    return pymysql.connect(
        host=values["host"],
        port=int(values["port"]),
        database=values["name"],
        user=values["username"],
        password=values["password"],
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
        connect_timeout=5
    )


def response(status, data):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
            "Access-Control-Allow-Methods": "GET,POST,PUT,PATCH,DELETE,OPTIONS"
        },
        "body": json.dumps(data, default=str)
    }


def parse_body(event):
    data = event.get("body")

    if not data:
        return {}

    if isinstance(data, dict):
        return data

    try:
        parsed = json.loads(data)
    except Exception:
        raise ValueError("Request body contains invalid JSON.")

    if not isinstance(parsed, dict):
        raise ValueError("Request body must be a JSON object.")

    return parsed


def get_path_id(event, name):
    value = (event.get("pathParameters") or {}).get("id")

    if not value or not str(value).isdigit():
        raise ValueError(f"{name} ID must be a valid integer.")

    return int(value)


def get_authorizer_context(event):
    context = (
        event.get("requestContext", {}).get("authorizer", {})
    )

    if not context:
        raise PermissionError("Authorization context is missing.")

    customer_id = context.get("customer_id")
    role = context.get("role")

    if not customer_id or not str(customer_id).isdigit():
        raise PermissionError("Authenticated customer identity is missing.")

    if role not in VALID_ROLES:
        raise PermissionError("Authenticated customer role is invalid.")

    return int(customer_id), role


def require_admin(event):
    customer_id, role = get_authorizer_context(event)

    if role != "ADMIN":
        raise PermissionError("Administrator access is required.")

    return customer_id, role


def require_own_customer(event, customer_id):
    authenticated_id, role = get_authorizer_context(event)

    if role == "ADMIN":
        return

    if authenticated_id != customer_id:
        raise PermissionError(
            "You are not authorized to access this customer."
        )


@log_endpoint
def create_customer(event):
    data = parse_body(event)

    allowed_fields = {"email", "name", "token"}
    unexpected = set(data) - allowed_fields
    if unexpected:
        raise ValueError(
            "Unsupported customer fields: "
            + ", ".join(sorted(unexpected))
            + "."
        )

    email = data.get("email")
    name = data.get("name")

    if not isinstance(email, str) or not email.strip():
        raise ValueError("Customer email is required.")
    email = email.strip()

    if len(email) > 255 or not re.fullmatch(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise ValueError("Customer email must be a valid email address.")

    if not isinstance(name, str) or not name.strip():
        raise ValueError("Customer name is required.")
    name = name.strip()

    if len(name) > 255:
        raise ValueError("Customer name must not exceed 255 characters.")

    token = data.get("token")
    generated_token = False

    if token is None:
        token = secrets.token_urlsafe(32)
        generated_token = True
    elif not isinstance(token, str) or not token.strip():
        raise ValueError("Customer token must be a non-empty string.")
    else:
        token = token.strip()

    if len(token) < 8:
        raise ValueError("Customer token must contain at least 8 characters.")

    token_hash = hash_token(token)
    conn = get_db_connection()

    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT customer_id FROM customers WHERE email=%s LIMIT 1",
                (email,)
            )
            if cur.fetchone():
                return response(
                    409,
                    {"message": "A customer with this email address already exists."}
                )

            cur.execute(
                "SELECT customer_id FROM customers WHERE token_hash=%s LIMIT 1",
                (token_hash,)
            )
            if cur.fetchone():
                return response(
                    409,
                    {"message": "This customer token is already assigned to another customer."}
                )

            cur.execute(
                """
                INSERT INTO customers (email, name, token_hash, role)
                VALUES (%s, %s, %s, 'USER')
                """,
                (email, name, token_hash)
            )
            customer_id = cur.lastrowid

        conn.commit()

        result = {
            "message": "Customer created successfully.",
            "customer_id": customer_id,
            "role": "USER"
        }

        if generated_token:
            result["token"] = token
            result["token_message"] = (
                "Store this token securely. It will not be returned again."
            )

        return response(201, result)

    except pymysql.IntegrityError:
        conn.rollback()
        return response(
            409,
            {"message": "Customer could not be created because the email or token already exists."}
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@log_endpoint
def list_customers(event):
    require_admin(event)

    conn = get_db_connection()

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    customer_id,
                    email,
                    name,
                    role,
                    created_at,
                    updated_at
                FROM customers
                ORDER BY customer_id
                """
            )
            customers = cur.fetchall()

        return response(200, {"customers": customers})

    finally:
        conn.close()


@log_endpoint
def get_customer(event):
    customer_id = get_path_id(event, "Customer")

    require_own_customer(event, customer_id)

    conn = get_db_connection()

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    customer_id,
                    email,
                    name,
                    role,
                    created_at,
                    updated_at
                FROM customers
                WHERE customer_id=%s
                """,
                (customer_id,)
            )
            customer = cur.fetchone()

        if not customer:
            return response(404, {"message": "Customer not found."})

        return response(200, {"customer": customer})

    finally:
        conn.close()


@log_endpoint
def update_customer(event):
    customer_id = get_path_id(event, "Customer")
    data = parse_body(event)
    authenticated_id, role = get_authorizer_context(event)

    if role != "ADMIN" and authenticated_id != customer_id:
        raise PermissionError(
            "You are not authorized to update this customer."
        )

    allowed_fields = {"name", "email", "token", "role"}
    unexpected = set(data) - allowed_fields
    if unexpected:
        raise ValueError(
            "Unsupported customer fields: "
            + ", ".join(sorted(unexpected))
            + "."
        )

    fields = []
    values = []

    if "name" in data:
        if not isinstance(data["name"], str) or not data["name"].strip():
            raise ValueError("Customer name must be a non-empty string.")
        name = data["name"].strip()
        if len(name) > 255:
            raise ValueError("Customer name must not exceed 255 characters.")
        fields.append("name=%s")
        values.append(name)

    if "email" in data:
        if not isinstance(data["email"], str) or not data["email"].strip():
            raise ValueError("Customer email must be a non-empty string.")
        email = data["email"].strip()
        if len(email) > 255 or not re.fullmatch(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
            raise ValueError("Customer email must be a valid email address.")
        fields.append("email=%s")
        values.append(email)

    token_hash = None
    if "token" in data:
        token = data["token"]
        if not isinstance(token, str) or not token.strip():
            raise ValueError("Customer token must be a non-empty string.")
        token = token.strip()
        if len(token) < 8:
            raise ValueError("Customer token must contain at least 8 characters.")
        token_hash = hash_token(token)
        fields.append("token_hash=%s")
        values.append(token_hash)

    if "role" in data:
        if role != "ADMIN":
            raise PermissionError(
                "Only administrators can change customer roles."
            )
        if not isinstance(data["role"], str):
            raise ValueError("Customer role must be a string.")
        new_role = data["role"].strip().upper()
        if new_role not in VALID_ROLES:
            raise ValueError(
                "Invalid role. Allowed roles: ADMIN, PRODUCT_OWNER, USER."
            )
        fields.append("role=%s")
        values.append(new_role)

    if not fields:
        raise ValueError("No valid fields were provided for update.")

    conn = get_db_connection()

    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT customer_id FROM customers WHERE customer_id=%s",
                (customer_id,)
            )
            if not cur.fetchone():
                return response(
                    404,
                    {"message": f"Customer {customer_id} was not found."}
                )

            if "email" in data:
                cur.execute(
                    """
                    SELECT customer_id FROM customers
                    WHERE email=%s AND customer_id<>%s LIMIT 1
                    """,
                    (email, customer_id)
                )
                if cur.fetchone():
                    return response(
                        409,
                        {"message": "The email address is already assigned to another customer."}
                    )

            if token_hash:
                cur.execute(
                    """
                    SELECT customer_id FROM customers
                    WHERE token_hash=%s AND customer_id<>%s LIMIT 1
                    """,
                    (token_hash, customer_id)
                )
                if cur.fetchone():
                    return response(
                        409,
                        {"message": "The provided token is already assigned to another customer."}
                    )

            cur.execute(
                f"UPDATE customers SET {','.join(fields)} WHERE customer_id=%s",
                values + [customer_id]
            )

        conn.commit()

        return response(
            200,
            {
                "message": "Customer updated successfully.",
                "customer_id": customer_id
            }
        )

    except pymysql.IntegrityError:
        conn.rollback()
        return response(
            409,
            {"message": "Customer could not be updated because the email or token already exists."}
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@log_endpoint
def delete_customer(event):
    customer_id = get_path_id(event, "Customer")

    require_admin(event)

    conn = get_db_connection()

    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT customer_id FROM customers WHERE customer_id=%s",
                (customer_id,)
            )

            if not cur.fetchone():
                return response(404, {"message": "Customer not found."})

            cur.execute(
                "DELETE FROM customers WHERE customer_id=%s",
                (customer_id,)
            )

        conn.commit()

        return response(
            200,
            {
                "message": "Customer deleted successfully.",
                "customer_id": customer_id
            }
        )

    except pymysql.IntegrityError:
        conn.rollback()
        return response(
            409,
            {"message": "Customer has existing orders."}
        )

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


@log_endpoint
def lambda_handler(event, context):
    request_id = getattr(context, "aws_request_id", "unknown") if context else "unknown"
    method = event.get("httpMethod", "").upper()
    path = event.get("path", "")

    try:
        if method == "OPTIONS":
            return response(204, {})

        if method == "POST" and (path.endswith("/customer") or path.endswith("/customers")):
            return create_customer(event)

        if method == "GET" and (path.endswith("/customer") or path.endswith("/customers")):
            return list_customers(event)

        if method == "GET" and ("/customer/" in path or "/customers/" in path):
            return get_customer(event)

        if method == "PUT" and ("/customer/" in path or "/customers/" in path):
            return update_customer(event)

        if method == "DELETE" and ("/customer/" in path or "/customers/" in path):
            return delete_customer(event)

        return response(
            404,
            {"message": f"API route not found for {method} {path}."}
        )

    except PermissionError as e:
        return response(403, {"message": str(e)})

    except ValueError as e:
        return response(400, {"message": str(e)})

    except pymysql.IntegrityError:
        return response(
            409,
            {"message": "Database constraint prevented the operation."}
        )

    except pymysql.MySQLError:
        logging.getLogger().exception(
            "Database error request_id=%s", request_id
        )
        return response(
            500,
            {
                "message": "The request could not be completed because of a database error.",
                "request_id": request_id
            }
        )

    except Exception:
        logging.getLogger().exception(
            "Unhandled API error request_id=%s", request_id
        )
        return response(
            500,
            {
                "message": "The request could not be completed because of an unexpected server error.",
                "request_id": request_id
            }
        )