import json
import logging
import os
import re
import hashlib
import secrets
import time
from decimal import Decimal
from functools import wraps

import boto3
import pymysql


VALID_ROLES = {"USER", "PRODUCT_OWNER", "ADMIN"}

VALID_ORDER_STATUSES = {
    "PENDING",
    "FAILED",
    "CONFIRMED",
    "PROCESSING",
    "SHIPPED",
    "DELIVERED",
    "CANCELLED"
}

ORDER_STATUS_TRANSITIONS = {
    "PENDING": {"CONFIRMED", "FAILED", "CANCELLED"},
    "FAILED": set(),
    "CONFIRMED": {"PROCESSING", "CANCELLED"},
    "PROCESSING": {"SHIPPED"},
    "SHIPPED": {"DELIVERED"},
    "DELIVERED": set(),
    "CANCELLED": set()
}


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
        found = {
            item["Name"]
            for item in parameters
        }

        missing = [
            name
            for name in names
            if name not in found
        ]

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
            "Access-Control-Allow-Headers":
                "Content-Type,Authorization",
            "Access-Control-Allow-Methods":
                "GET,POST,PUT,PATCH,DELETE,OPTIONS"
        },
        "body": json.dumps(
            data,
            default=str
        )
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
        raise ValueError(
            "Request body contains invalid JSON."
        )

    if not isinstance(parsed, dict):
        raise ValueError(
            "Request body must be a JSON object."
        )

    return parsed


def get_path_id(event, name):
    value = (
        event.get("pathParameters") or {}
    ).get("id")

    if not value or not str(value).isdigit():
        raise ValueError(
            f"{name} ID must be a valid integer."
        )

    return int(value)


def get_authorizer_context(event):
    context = (
        event
        .get("requestContext", {})
        .get("authorizer", {})
    )

    if not context:
        raise PermissionError(
            "Authorization context is missing."
        )

    customer_id = context.get("customer_id")
    role = context.get("role")

    if not customer_id or not str(customer_id).isdigit():
        raise PermissionError(
            "Authenticated customer identity is missing."
        )

    if role not in VALID_ROLES:
        raise PermissionError(
            "Authenticated customer role is invalid."
        )

    return int(customer_id), role


def require_admin(event):
    customer_id, role = get_authorizer_context(event)

    if role != "ADMIN":
        raise PermissionError(
            "Administrator access is required."
        )

    return customer_id, role


def require_product_owner_or_admin(event):
    customer_id, role = get_authorizer_context(event)

    if role not in {
        "PRODUCT_OWNER",
        "ADMIN"
    }:
        raise PermissionError(
            "Product owner or administrator access is required."
        )

    return customer_id, role


def require_own_customer(event, customer_id):
    authenticated_id, role = get_authorizer_context(event)

    if role == "ADMIN":
        return

    if authenticated_id != customer_id:
        raise PermissionError(
            "You are not authorized to access this customer."
        )


def publish_event(event_type, detail):
    logger = logging.getLogger()

    environment = get_environment()

    event_bus = (
        f"cloudmart-{environment}-event-bus"
    )

    events = boto3.client("events")

    try:
        result = events.put_events(
            Entries=[
                {
                    "EventBusName": event_bus,
                    "Source": "cloudmart.order",
                    "DetailType": event_type,
                    "Detail": json.dumps(
                        detail,
                        default=str
                    )
                }
            ]
        )

        if result.get("FailedEntryCount", 0) > 0:
            logger.error(
                "EventBridge publish failed "
                "event_type=%s result=%s",
                event_type,
                result
            )

        return result

    except Exception:
        logger.exception(
            "EventBridge publish exception "
            "event_type=%s",
            event_type
        )
        raise



def publish_metric(metric_name, value=1):
    """
    Publish a single CloudWatch operational metric.

    Metric publishing is intentionally best-effort: a CloudWatch publishing
    failure must not turn an otherwise successful database operation into an
    API failure.
    """
    logger = logging.getLogger()

    try:
        cloudwatch = boto3.client("cloudwatch")

        cloudwatch.put_metric_data(
            Namespace="CloudMart/Operations",
            MetricData=[
                {
                    "MetricName": metric_name,
                    "Dimensions": [
                        {
                            "Name": "Environment",
                            "Value": get_environment()
                        }
                    ],
                    "Value": value,
                    "Unit": "Count"
                }
            ]
        )

        logger.info(
            "CloudWatch metric published metric=%s value=%s",
            metric_name,
            value
        )

    except Exception:
        logger.exception(
            "CloudWatch metric publishing failed metric=%s value=%s",
            metric_name,
            value
        )


def publish_order_failure_metric():
    publish_metric("OrdersFailed")


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

        return response(
            200,
            {
                "customers": customers
            }
        )

    finally:
        conn.close()


@log_endpoint
def get_customer(event):
    customer_id = get_path_id(
        event,
        "Customer"
    )

    require_own_customer(
        event,
        customer_id
    )

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
            return response(
                404,
                {
                    "message":
                        "Customer not found."
                }
            )

        return response(
            200,
            {
                "customer":
                    customer
            }
        )

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
    customer_id = get_path_id(
        event,
        "Customer"
    )

    require_admin(event)

    conn = get_db_connection()

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT customer_id
                FROM customers
                WHERE customer_id=%s
                """,
                (customer_id,)
            )

            if not cur.fetchone():
                return response(
                    404,
                    {
                        "message":
                            "Customer not found."
                    }
                )

            cur.execute(
                """
                DELETE FROM customers
                WHERE customer_id=%s
                """,
                (customer_id,)
            )

        conn.commit()

        return response(
            200,
            {
                "message":
                    "Customer deleted successfully.",
                "customer_id":
                    customer_id
            }
        )

    except pymysql.IntegrityError:
        conn.rollback()

        return response(
            409,
            {
                "message":
                    "Customer has existing orders."
            }
        )

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


@log_endpoint
def create_order(event):
    authenticated_id, role = get_authorizer_context(event)

    data = parse_body(event)

    allowed_fields = {"customer_id", "items"}
    unexpected = set(data) - allowed_fields

    if unexpected:
        raise ValueError(
            "Unsupported order fields: "
            + ", ".join(sorted(unexpected))
            + "."
        )

    if "items" not in data:
        raise ValueError(
            "Order items are required. Provide at least one product."
        )

    if not isinstance(data["items"], list):
        raise ValueError("items must be an array of order items.")

    if not data["items"]:
        raise ValueError(
            "Order items are required. Provide at least one product."
        )
    
    if "customer_id" in data:
        if isinstance(data["customer_id"], bool):
            raise ValueError("customer_id must be a valid positive integer.")

        try:
            customer_id = int(data["customer_id"])
        except (TypeError, ValueError):
            raise ValueError("customer_id must be a valid positive integer.")

        if customer_id <= 0:
            raise ValueError("customer_id must be a valid positive integer.")

        if role == "USER" and customer_id != authenticated_id:
            raise PermissionError(
                "You are not authorized to create an order for this customer."
            )
    else:
        raise ValueError(
            "customer_id is required when creating an order."
        )

    normalized_items = {}

    for index, item in enumerate(
        data["items"],
        start=1
    ):
        if not isinstance(item, dict):
            raise ValueError(
                f"Order item {index} must be a JSON object."
            )

        if "product_id" not in item:
            raise ValueError(
                f"Order item {index} requires product_id."
            )

        if "quantity" not in item:
            raise ValueError(
                f"Order item {index} requires quantity."
            )

        try:
            product_id = int(
                item["product_id"]
            )

            quantity = int(
                item["quantity"]
            )

        except (TypeError, ValueError):
            raise ValueError(
                f"Order item {index} contains an invalid integer."
            )

        if product_id <= 0:
            raise ValueError(
                f"Order item {index} product_id "
                "must be greater than 0."
            )

        if quantity <= 0:
            raise ValueError(
                f"Order item {index} quantity "
                "must be greater than 0."
            )

        normalized_items[product_id] = (
            normalized_items.get(
                product_id,
                0
            )
            + quantity
        )

    conn = get_db_connection()

    order_id = None
    inventory_updates = 0
    low_stock_events = 0

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    customer_id,
                    name,
                    email
                FROM customers
                WHERE customer_id=%s
                """,
                (customer_id,)
            )

            customer = cur.fetchone()

            if not customer:
                return response(
                    404,
                    {
                        "message":
                            f"Customer {customer_id} not found."
                    }
                )

            cur.execute(
                """
                INSERT INTO orders
                (
                    customer_id,
                    order_number,
                    status,
                    total_amount
                )
                VALUES (%s, '', 'PENDING', 0)
                """,
                (customer_id,)
            )

            order_id = cur.lastrowid

            order_number = (
                f"ORD-{order_id:06d}"
            )

            cur.execute(
                """
                UPDATE orders
                SET order_number=%s
                WHERE order_id=%s
                """,
                (
                    order_number,
                    order_id
                )
            )

            cur.execute(
                """
                INSERT INTO order_logs
                (
                    order_id,
                    event_type,
                    old_status,
                    new_status,
                    message
                )
                VALUES (%s,%s,%s,%s,%s)
                """,
                (
                    order_id,
                    "CREATED",
                    None,
                    "PENDING",
                    "Order created and inventory validation started"
                )
            )

            products = []
            total = Decimal("0.00")

            for product_id, quantity in normalized_items.items():
                cur.execute(
                    """
                    SELECT
                        p.product_id,
                        p.name,
                        p.price,
                        i.quantity_available,
                        i.reorder_threshold
                    FROM products p
                    JOIN inventory i
                        ON p.product_id=i.product_id
                    WHERE p.product_id=%s
                      AND p.is_active=TRUE
                    FOR UPDATE
                    """,
                    (product_id,)
                )

                product = cur.fetchone()

                if not product:
                    raise ValueError(
                        f"Product {product_id} is unavailable. "
                        "The product may not exist, may be inactive, "
                        "or may not have an inventory record."
                    )

                available = int(
                    product["quantity_available"]
                )

                unit_price = Decimal(
                    str(product["price"])
                )

                item_total = (
                    unit_price * quantity
                )

                total += item_total

                products.append(
                    {
                        "product_id":
                            product_id,
                        "product_name":
                            product["name"],
                        "quantity":
                            quantity,
                        "unit_price":
                            unit_price,
                        "total_price":
                            item_total,
                        "available":
                            available
                    }
                )

            failed_product = None

            for product in products:
                if (
                    product["quantity"]
                    > product["available"]
                ):
                    failed_product = product
                    break

            if failed_product:
                reason = (
                    f"Insufficient stock for product "
                    f"'{failed_product['product_name']}'. "
                    f"Requested "
                    f"{failed_product['quantity']}, "
                    f"but only "
                    f"{failed_product['available']} "
                    f"is available."
                )

                cur.execute(
                    """
                    UPDATE orders
                    SET
                        status='FAILED',
                        total_amount=0
                    WHERE order_id=%s
                    """,
                    (order_id,)
                )

                cur.execute(
                    """
                    INSERT INTO order_logs
                    (
                        order_id,
                        event_type,
                        old_status,
                        new_status,
                        message
                    )
                    VALUES (%s,%s,%s,%s,%s)
                    """,
                    (
                        order_id,
                        "STATUS_CHANGED",
                        "PENDING",
                        "FAILED",
                        reason
                    )
                )

                conn.commit()

                publish_order_failure_metric()

                try:
                    publish_event(
                        "Order Failed",
                        {
                            "order_id":
                                order_id,
                            "order_number":
                                order_number,
                            "customer": {
                                "customer_id":
                                    customer["customer_id"],
                                "name":
                                    customer["name"],
                                "email":
                                    customer["email"]
                            },
                            "previous_status":
                                "PENDING",
                            "status":
                                "FAILED",
                            "reason":
                                reason
                        }
                    )

                except Exception:
                    logging.getLogger().exception(
                        "Failed order committed but "
                        "event publishing failed."
                    )

                return response(
                    201,
                    {
                        "message":
                            "Order creation failed.",
                        "order_id":
                            order_id,
                        "order_number":
                            order_number,
                        "status":
                            "FAILED",
                        "reason":
                            reason
                    }
                )

            order_items = []

            for product in products:
                product_id = (
                    product["product_id"]
                )

                quantity = (
                    product["quantity"]
                )

                cur.execute(
                    """
                    INSERT INTO order_items
                    (
                        order_id,
                        product_id,
                        quantity,
                        unit_price,
                        total_price
                    )
                    VALUES (%s,%s,%s,%s,%s)
                    """,
                    (
                        order_id,
                        product_id,
                        quantity,
                        product["unit_price"],
                        product["total_price"]
                    )
                )

                cur.execute(
                    """
                    UPDATE inventory
                    SET quantity_available =
                        quantity_available - %s
                    WHERE product_id=%s
                      AND quantity_available >= %s
                    """,
                    (
                        quantity,
                        product_id,
                        quantity
                    )
                )

                if cur.rowcount != 1:
                    raise RuntimeError(
                        f"Inventory update failed for "
                        f"product {product_id}."
                    )

                cur.execute(
                    """
                    SELECT quantity_available
                    FROM inventory
                    WHERE product_id=%s
                    FOR UPDATE
                    """,
                    (product_id,)
                )

                inventory_after = (
                    cur.fetchone()
                )

                if not inventory_after:
                    raise RuntimeError(
                        f"Inventory verification failed for "
                        f"product {product_id}."
                    )

                after_quantity = int(
                    inventory_after[
                        "quantity_available"
                    ]
                )

                expected_quantity = (
                    product["available"]
                    - quantity
                )

                if (
                    after_quantity
                    != expected_quantity
                ):
                    raise RuntimeError(
                        f"Inventory verification failed for "
                        f"product {product_id}."
                    )

                inventory_updates += 1

                reorder_threshold = int(
                    product["reorder_threshold"]
                )

                if (
                    int(product["available"]) > reorder_threshold
                    and after_quantity <= reorder_threshold
                ):
                    low_stock_events += 1

                logging.getLogger().info(
                    "Inventory update verified product_id=%s "
                    "previous_quantity=%s requested_quantity=%s "
                    "new_quantity=%s reorder_threshold=%s "
                    "low_stock_transition=%s",
                    product_id,
                    product["available"],
                    quantity,
                    after_quantity,
                    reorder_threshold,
                    (
                        int(product["available"]) > reorder_threshold
                        and after_quantity <= reorder_threshold
                    )
                )

                order_items.append(
                    {
                        "product_id":
                            product_id,
                        "product_name":
                            product["product_name"],
                        "quantity":
                            quantity,
                        "unit_price":
                            product["unit_price"],
                        "total_price":
                            product["total_price"]
                    }
                )

            cur.execute(
                """
                UPDATE orders
                SET
                    status='CONFIRMED',
                    total_amount=%s
                WHERE order_id=%s
                """,
                (
                    total,
                    order_id
                )
            )

            cur.execute(
                """
                INSERT INTO order_logs
                (
                    order_id,
                    event_type,
                    old_status,
                    new_status,
                    message
                )
                VALUES (%s,%s,%s,%s,%s)
                """,
                (
                    order_id,
                    "STATUS_CHANGED",
                    "PENDING",
                    "CONFIRMED",
                    "Order confirmed and inventory reserved successfully"
                )
            )

        conn.commit()

        publish_metric("OrdersPlaced")

        if low_stock_events:
            publish_metric(
                "LowStockEvents",
                low_stock_events
            )

        try:
            publish_event(
                "Order Confirmed",
                {
                    "order_id":
                        order_id,
                    "order_number":
                        order_number,
                    "customer": {
                        "customer_id":
                            customer["customer_id"],
                        "name":
                            customer["name"],
                        "email":
                            customer["email"]
                    },
                    "previous_status":
                        "PENDING",
                    "status":
                        "CONFIRMED",
                    "items":
                        order_items,
                    "total_amount":
                        total
                }
            )

        except Exception:
            logging.getLogger().exception(
                "Confirmed order committed but "
                "event publishing failed."
            )

        return response(
            201,
            {
                "message":
                    "Order created successfully.",
                "order_id":
                    order_id,
                "order_number":
                    order_number,
                "status":
                    "CONFIRMED",
                "total_amount":
                    total,
                "items":
                    order_items
            }
        )

    except Exception:
        conn.rollback()
        publish_order_failure_metric()
        raise

    finally:
        conn.close()


@log_endpoint
def list_orders(event):
    authenticated_id, role = get_authorizer_context(event)

    conn = get_db_connection()

    try:
        with conn.cursor() as cur:
            if role == "USER":
                cur.execute(
                    """
                    SELECT
                        order_id,
                        customer_id,
                        order_number,
                        status,
                        total_amount,
                        created_at,
                        updated_at
                    FROM orders
                    WHERE customer_id=%s
                    ORDER BY order_id DESC
                    """,
                    (authenticated_id,)
                )

            else:
                cur.execute(
                    """
                    SELECT
                        order_id,
                        customer_id,
                        order_number,
                        status,
                        total_amount,
                        created_at,
                        updated_at
                    FROM orders
                    ORDER BY order_id DESC
                    """
                )

            orders = cur.fetchall()

        return response(
            200,
            {
                "orders":
                    orders
            }
        )

    finally:
        conn.close()


@log_endpoint
def list_customer_orders(event):
    customer_id = get_path_id(
        event,
        "Customer"
    )

    require_own_customer(
        event,
        customer_id
    )

    conn = get_db_connection()

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT customer_id
                FROM customers
                WHERE customer_id=%s
                """,
                (customer_id,)
            )

            if not cur.fetchone():
                return response(
                    404,
                    {
                        "message":
                            "Customer not found."
                    }
                )

            cur.execute(
                """
                SELECT
                    order_id,
                    customer_id,
                    order_number,
                    status,
                    total_amount,
                    created_at,
                    updated_at
                FROM orders
                WHERE customer_id=%s
                ORDER BY order_id DESC
                """,
                (customer_id,)
            )

            orders = cur.fetchall()

        return response(
            200,
            {
                "orders":
                    orders
            }
        )

    finally:
        conn.close()


@log_endpoint
def get_order(event):
    order_id = get_path_id(
        event,
        "Order"
    )

    authenticated_id, role = get_authorizer_context(event)

    conn = get_db_connection()

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    o.order_id,
                    o.customer_id,
                    c.name AS customer_name,
                    c.email AS customer_email,
                    o.order_number,
                    o.status,
                    o.total_amount,
                    o.created_at,
                    o.updated_at
                FROM orders o
                JOIN customers c
                    ON o.customer_id=c.customer_id
                WHERE o.order_id=%s
                """,
                (order_id,)
            )

            order = cur.fetchone()

            if not order:
                return response(
                    404,
                    {
                        "message":
                            "Order not found."
                    }
                )

            if (
                role != "ADMIN"
                and role != "PRODUCT_OWNER"
                and order["customer_id"]
                    != authenticated_id
            ):
                raise PermissionError(
                    "You are not authorized to access this order."
                )

            cur.execute(
                """
                SELECT
                    oi.order_item_id,
                    oi.product_id,
                    p.name AS product_name,
                    oi.quantity,
                    oi.unit_price,
                    oi.total_price
                FROM order_items oi
                JOIN products p
                    ON oi.product_id=p.product_id
                WHERE oi.order_id=%s
                ORDER BY oi.order_item_id
                """,
                (order_id,)
            )

            order["items"] = cur.fetchall()

            cur.execute(
                """
                SELECT
                    log_id,
                    event_type,
                    old_status,
                    new_status,
                    message,
                    created_at
                FROM order_logs
                WHERE order_id=%s
                ORDER BY log_id
                """,
                (order_id,)
            )

            order["logs"] = cur.fetchall()

        return response(
            200,
            {
                "order":
                    order
            }
        )

    finally:
        conn.close()


@log_endpoint
def update_order(event):
    require_product_owner_or_admin(event)

    order_id = get_path_id(
        event,
        "Order"
    )

    data = parse_body(event)

    if not data.get("status"):
        raise ValueError(
            "status is required."
        )

    new_status = str(
        data["status"]
    ).strip().upper()

    if new_status == "CANCELLED":
        raise ValueError(
            "Use PATCH /order/{id} to cancel an order."
        )

    if new_status not in VALID_ORDER_STATUSES:
        raise ValueError(
            "Invalid order status. Allowed statuses: "
            + ", ".join(
                sorted(VALID_ORDER_STATUSES)
            )
        )

    conn = get_db_connection()

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    o.status,
                    o.order_number,
                    o.customer_id,
                    c.name,
                    c.email
                FROM orders o
                JOIN customers c
                    ON o.customer_id=c.customer_id
                WHERE o.order_id=%s
                FOR UPDATE
                """,
                (order_id,)
            )

            order = cur.fetchone()

            if not order:
                return response(
                    404,
                    {
                        "message":
                            "Order not found."
                    }
                )

            old_status = order["status"]

            if new_status not in (
                ORDER_STATUS_TRANSITIONS.get(
                    old_status,
                    set()
                )
            ):
                raise ValueError(
                    f"Cannot change order status "
                    f"from {old_status} to {new_status}."
                )

            cur.execute(
                """
                UPDATE orders
                SET status=%s
                WHERE order_id=%s
                """,
                (
                    new_status,
                    order_id
                )
            )

            cur.execute(
                """
                INSERT INTO order_logs
                (
                    order_id,
                    event_type,
                    old_status,
                    new_status,
                    message
                )
                VALUES (%s,%s,%s,%s,%s)
                """,
                (
                    order_id,
                    "STATUS_CHANGED",
                    old_status,
                    new_status,
                    f"Order status changed from "
                    f"{old_status} to {new_status}"
                )
            )

        conn.commit()

        try:
            publish_event(
                f"Order {new_status.title()}",
                {
                    "order_id":
                        order_id,
                    "order_number":
                        order["order_number"],
                    "customer": {
                        "customer_id":
                            order["customer_id"],
                        "name":
                            order["name"],
                        "email":
                            order["email"]
                    },
                    "previous_status":
                        old_status,
                    "status":
                        new_status
                }
            )

        except Exception:
            logging.getLogger().exception(
                "Order status changed but "
                "event publishing failed."
            )

        return response(
            200,
            {
                "message":
                    "Order updated successfully.",
                "order_id":
                    order_id,
                "order_number":
                    order["order_number"],
                "status":
                    new_status
            }
        )

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


@log_endpoint
def cancel_order(event):
    authenticated_id, role = get_authorizer_context(event)

    order_id = get_path_id(
        event,
        "Order"
    )

    data = parse_body(event)

    requested_status = str(
        data.get(
            "status",
            "CANCELLED"
        )
    ).strip().upper()

    if requested_status != "CANCELLED":
        raise ValueError(
            "PATCH /order/{id} only supports "
            "status=CANCELLED."
        )

    conn = get_db_connection()
    inventory_restorations = 0

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    o.status,
                    o.order_number,
                    o.customer_id,
                    c.name,
                    c.email
                FROM orders o
                JOIN customers c
                    ON o.customer_id=c.customer_id
                WHERE o.order_id=%s
                FOR UPDATE
                """,
                (order_id,)
            )

            order = cur.fetchone()

            if not order:
                return response(
                    404,
                    {
                        "message":
                            "Order not found."
                    }
                )

            if (
                role != "ADMIN"
                and role != "PRODUCT_OWNER"
                and order["customer_id"]
                    != authenticated_id
            ):
                raise PermissionError(
                    "You are not authorized to cancel this order."
                )

            current_status = order["status"]

            if current_status == "CANCELLED":
                return response(
                    400,
                    {
                        "message":
                            "Order is already cancelled."
                    }
                )

            if current_status in {
                "SHIPPED",
                "DELIVERED"
            }:
                return response(
                    400,
                    {
                        "message":
                            f"Cannot cancel an order with status "
                            f"{current_status}."
                    }
                )

            if current_status == "FAILED":
                return response(
                    400,
                    {
                        "message":
                            "A failed order cannot be cancelled."
                    }
                )

            cur.execute(
                """
                SELECT
                    product_id,
                    quantity
                FROM order_items
                WHERE order_id=%s
                FOR UPDATE
                """,
                (order_id,)
            )

            items = cur.fetchall()

            for item in items:
                cur.execute(
                    """
                    UPDATE inventory
                    SET quantity_available =
                        quantity_available + %s
                    WHERE product_id=%s
                    """,
                    (
                        int(item["quantity"]),
                        item["product_id"]
                    )
                )

                if cur.rowcount != 1:
                    raise RuntimeError(
                        "Inventory restoration failed for "
                        f"product {item['product_id']}."
                    )

                inventory_restorations += 1

            cur.execute(
                """
                UPDATE orders
                SET status='CANCELLED'
                WHERE order_id=%s
                """,
                (order_id,)
            )

            cur.execute(
                """
                INSERT INTO order_logs
                (
                    order_id,
                    event_type,
                    old_status,
                    new_status,
                    message
                )
                VALUES (%s,%s,%s,%s,%s)
                """,
                (
                    order_id,
                    "CANCELLED",
                    current_status,
                    "CANCELLED",
                    "Order cancelled and inventory restored"
                )
            )

        conn.commit()

        try:
            publish_event(
                "Order Cancelled",
                {
                    "order_id":
                        order_id,
                    "order_number":
                        order["order_number"],
                    "customer": {
                        "customer_id":
                            order["customer_id"],
                        "name":
                            order["name"],
                        "email":
                            order["email"]
                    },
                    "previous_status":
                        current_status,
                    "status":
                        "CANCELLED"
                }
            )

        except Exception:
            logging.getLogger().exception(
                "Order cancelled but event publishing failed."
            )

        return response(
            200,
            {
                "message":
                    "Order cancelled successfully. "
                    "Inventory was restored.",
                "order_id":
                    order_id,
                "order_number":
                    order["order_number"],
                "status":
                    "CANCELLED"
            }
        )

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


@log_endpoint
def lambda_handler(event, context):
    request_id = (
        getattr(
            context,
            "aws_request_id",
            "unknown"
        )
        if context
        else "unknown"
    )

    method = event.get(
        "httpMethod",
        ""
    ).upper()

    path = event.get(
        "path",
        ""
    )

    try:
        if method == "OPTIONS":
            return response(
                204,
                {}
            )

        if (
            method == "POST"
            and path.endswith("/customer")
        ):
            return create_customer(event)

        if (
            method == "GET"
            and path.endswith("/customer")
        ):
            return list_customers(event)

        if (
            method == "GET"
            and "/customer/" in path
        ):
            return get_customer(event)

        if (
            method == "PUT"
            and "/customer/" in path
        ):
            return update_customer(event)

        if (
            method == "DELETE"
            and "/customer/" in path
        ):
            return delete_customer(event)

        if (
            method == "POST"
            and path.endswith("/order")
        ):
            return create_order(event)

        if (
            method == "GET"
            and path.endswith("/order")
        ):
            return list_orders(event)

        if (
            method == "GET"
            and "/order/customer/" in path
        ):
            return list_customer_orders(event)

        if (
            method == "PATCH"
            and "/order/" in path
        ):
            return cancel_order(event)

        if (
            method == "GET"
            and "/order/" in path
        ):
            return get_order(event)

        if (
            method == "PUT"
            and "/order/" in path
        ):
            return update_order(event)

        return response(
            404,
            {
                "message":
                    f"API route not found for "
                    f"{method} {path}."
            }
        )

    except PermissionError as e:
        return response(
            403,
            {
                "message":
                    str(e)
            }
        )

    except ValueError as e:
        return response(
            400,
            {
                "message":
                    str(e)
            }
        )

    except pymysql.IntegrityError:
        return response(
            409,
            {
                "message":
                    "Database constraint prevented the operation."
            }
        )

    except pymysql.MySQLError:
        logging.getLogger().exception(
            "Database error request_id=%s",
            request_id
        )

        return response(
            500,
            {
                "message":
                    "The request could not be completed because of a database error.",
                "request_id":
                    request_id
            }
        )

    except Exception:
        logging.getLogger().exception(
            "Unhandled API error request_id=%s",
            request_id
        )

        return response(
            500,
            {
                "message":
                    "The request could not be completed because of an unexpected server error.",
                "request_id":
                    request_id
            }
        )