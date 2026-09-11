import json
import logging
import os
import hashlib
import secrets
import time
from decimal import Decimal
from functools import wraps

import boto3
import pymysql

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ssm = boto3.client("ssm")
events = boto3.client("events")

ENV = os.getenv("ENVIRONMENT", "dev")
EVENT_BUS = f"cloudmart-{ENV}-event-bus"

_connection = None

VALID_ORDER_STATUSES = {
    "PENDING",
    "CONFIRMED",
    "PROCESSING",
    "SHIPPED",
    "DELIVERED",
    "CANCELLED"
}

ORDER_STATUS_TRANSITIONS = {
    "PENDING": {"CONFIRMED", "CANCELLED"},
    "CONFIRMED": {"PROCESSING", "CANCELLED"},
    "PROCESSING": {"SHIPPED"},
    "SHIPPED": {"DELIVERED"},
    "DELIVERED": set(),
    "CANCELLED": set()
}


def hash_token(token):
    if not token or not isinstance(token, str):
        raise ValueError("Customer token must be a non-empty string.")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def log_endpoint(func):
    @wraps(func)
    def wrapper(event, *args, **kwargs):
        start = time.time()
        method = event.get("httpMethod", "").upper() if isinstance(event, dict) else ""
        path = event.get("path", "") if isinstance(event, dict) else ""
        logger.info("START function=%s method=%s path=%s", func.__name__, method, path)
        try:
            result = func(event, *args, **kwargs)
            status = result.get("statusCode") if isinstance(result, dict) else None
            logger.info("END function=%s status=%s duration_ms=%.2f", func.__name__, status, (time.time() - start) * 1000)
            return result
        except Exception:
            logger.exception("FAILED function=%s duration_ms=%.2f", func.__name__, (time.time() - start) * 1000)
            raise
    return wrapper


def db():
    global _connection

    logger.info("Database connection check started environment=%s", ENV)

    if _connection:
        try:
            _connection.ping(reconnect=True)
            logger.info("Reusing existing database connection")
            return _connection
        except Exception:
            logger.warning("Existing database connection is unavailable; creating a new connection", exc_info=True)
            _connection = None

    names = [
        f"/app/{ENV}/database/host",
        f"/app/{ENV}/database/port",
        f"/app/{ENV}/database/name",
        f"/app/{ENV}/database/username",
        f"/app/{ENV}/database/password"
    ]

    logger.info("Reading database configuration from SSM parameters")
    response_data = ssm.get_parameters(Names=names, WithDecryption=True)
    params = response_data.get("Parameters", [])

    if len(params) != len(names):
        found = {x["Name"] for x in params}
        missing = [name for name in names if name not in found]
        logger.error("Missing database SSM parameters: %s", missing)
        raise RuntimeError(f"Database configuration is incomplete. Missing parameters: {', '.join(missing)}")

    p = {x["Name"].split("/")[-1]: x["Value"] for x in params}

    logger.info("Connecting to database host=%s port=%s database=%s", p["host"], p["port"], p["name"])
    try:
        _connection = pymysql.connect(
            host=p["host"],
            port=int(p["port"]),
            database=p["name"],
            user=p["username"],
            password=p["password"],
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=False,
            connect_timeout=5
        )
        logger.info("Database connection established successfully")
        return _connection
    except Exception:
        logger.exception("Database connection failed host=%s port=%s database=%s", p["host"], p["port"], p["name"])
        raise




def response(status, data):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
            "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS"
        },
        "body": json.dumps(data, default=str)
    }


def body(event):
    data = event.get("body")

    if not data:
        return {}

    if isinstance(data, dict):
        return data

    try:
        return json.loads(data)
    except Exception:
        raise ValueError("Invalid JSON body.")


def id_from_path(event, name):
    value = (event.get("pathParameters") or {}).get("id")

    if not value or not str(value).isdigit():
        raise ValueError(f"{name} ID must be a valid integer.")

    return int(value)


def publish_event(event_type, detail):
    logger.info("EventBridge publish started event_type=%s event_bus=%s", event_type, EVENT_BUS)
    try:
        result = events.put_events(
            Entries=[
                {
                    "EventBusName": EVENT_BUS,
                    "Source": "cloudmart.order",
                    "DetailType": event_type,
                    "Detail": json.dumps(detail, default=str)
                }
            ]
        )
        failed_count = result.get("FailedEntryCount", 0)
        if failed_count > 0:
            logger.error("EventBridge publish failed event_type=%s failed_count=%s result=%s", event_type, failed_count, result)
        else:
            logger.info("EventBridge publish completed event_type=%s failed_count=0", event_type)
        return result
    except Exception:
        logger.exception("EventBridge publish exception event_type=%s", event_type)
        raise




@log_endpoint
def create_customer(event):
    logger.info("Customer creation: parsing request body")
    data = body(event)

    email = str(data.get("email", "")).strip()
    name = str(data.get("name", "")).strip()

    if not email:
        raise ValueError("Customer email is required.")
    if not name:
        raise ValueError("Customer name is required.")
    if "token_hash" in data:
        raise ValueError("token_hash must not be provided. Send token instead; the API will hash it securely.")

    supplied_token = data.get("token")
    generated_token = False
    if supplied_token is None or supplied_token == "":
        supplied_token = secrets.token_urlsafe(32)
        generated_token = True
        logger.info("Customer token was not supplied; a secure token was generated")
    else:
        logger.info("Customer token was supplied and will be hashed before storage")

    token_hash = hash_token(supplied_token)
    logger.info("Customer token hashing completed; plaintext token is not logged or stored")

    conn = db()
    try:
        with conn.cursor() as cur:
            logger.info("Creating customer email=%s name=%s", email, name)
            cur.execute(
                """
                INSERT INTO customers
                (email, name, token_hash)
                VALUES (%s, %s, %s)
                """,
                (email, name, token_hash)
            )
            customer_id = cur.lastrowid
            logger.info("Customer INSERT completed customer_id=%s", customer_id)

        conn.commit()
        logger.info("Customer transaction committed customer_id=%s", customer_id)

        result = {
            "message": "Customer created successfully. Customer token has been securely hashed and stored.",
            "customer_id": customer_id
        }
        if generated_token:
            result["token"] = supplied_token
            result["token_message"] = "This token was generated by the API. Store it securely; it will not be returned again."

        logger.info("Customer creation completed successfully customer_id=%s", customer_id)
        return response(201, result)

    except pymysql.IntegrityError as e:
        conn.rollback()
        logger.error("Customer creation integrity error email=%s error=%s", email, str(e))
        return response(409, {"message": "Customer could not be created because the email already exists."})
    except Exception:
        conn.rollback()
        logger.exception("Customer creation failed email=%s; transaction rolled back", email)
        raise




@log_endpoint
def list_customers(event):
    conn = db()

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                customer_id,
                email,
                name,
                created_at,
                updated_at
            FROM customers
            ORDER BY customer_id
            """
        )

        return response(
            200,
            {"customers": cur.fetchall()}
        )


@log_endpoint
def get_customer(event):
    customer_id = id_from_path(event, "Customer")
    conn = db()

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                customer_id,
                email,
                name,
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
            {"message": "Customer not found."}
        )

    return response(
        200,
        {"customer": customer}
    )


@log_endpoint
def update_customer(event):
    customer_id = id_from_path(event, "Customer")
    data = body(event)
    logger.info("Customer update started customer_id=%s", customer_id)

    fields = []
    values = []

    if "name" in data:
        name = str(data["name"]).strip()
        if not name:
            raise ValueError("Customer name cannot be empty.")
        fields.append("name=%s")
        values.append(name)

    if "email" in data:
        email = str(data["email"]).strip()
        if not email:
            raise ValueError("Customer email cannot be empty.")
        fields.append("email=%s")
        values.append(email)

    if "token_hash" in data:
        raise ValueError("token_hash must not be provided. Send token instead; the API will hash it securely.")

    if "token" in data:
        token = data["token"]
        fields.append("token_hash=%s")
        values.append(hash_token(token))
        logger.info("Customer token update hashed successfully customer_id=%s", customer_id)

    if not fields:
        raise ValueError("No fields to update. Provide name, email, or token.")

    values.append(customer_id)
    conn = db()

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT customer_id FROM customers WHERE customer_id=%s", (customer_id,))
            if not cur.fetchone():
                logger.warning("Customer update failed because customer_id=%s was not found", customer_id)
                return response(404, {"message": "Customer not found."})

            logger.info("Updating customer fields customer_id=%s fields=%s", customer_id, [f.split("=")[0] for f in fields])
            cur.execute(f"UPDATE customers SET {','.join(fields)} WHERE customer_id=%s", values)
            logger.info("Customer UPDATE completed customer_id=%s rows=%s", customer_id, cur.rowcount)

        conn.commit()
        logger.info("Customer update transaction committed customer_id=%s", customer_id)
        return response(200, {"message": "Customer updated successfully.", "customer_id": customer_id})

    except pymysql.IntegrityError as e:
        conn.rollback()
        logger.error("Customer update integrity error customer_id=%s error=%s", customer_id, str(e))
        return response(409, {"message": "Customer could not be updated because the email already exists."})
    except Exception:
        conn.rollback()
        logger.exception("Customer update failed customer_id=%s; transaction rolled back", customer_id)
        raise




@log_endpoint
def delete_customer(event):
    customer_id = id_from_path(event, "Customer")
    conn = db()

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
                    {"message": "Customer not found."}
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


@log_endpoint
def create_order(event):
    logger.info("Order creation: parsing request body")
    data = body(event)

    if "customer_id" not in data:
        raise ValueError("customer_id is required.")
    if not data.get("items"):
        raise ValueError("items are required and must contain at least one product.")

    try:
        customer_id = int(data["customer_id"])
    except (TypeError, ValueError):
        raise ValueError("customer_id must be a valid integer.")

    conn = db()
    order_id = None

    try:
        with conn.cursor() as cur:
            logger.info("Checking customer customer_id=%s", customer_id)
            cur.execute(
                """
                SELECT customer_id, name, email
                FROM customers
                WHERE customer_id=%s
                """,
                (customer_id,)
            )
            customer = cur.fetchone()

            if not customer:
                logger.warning("Order creation stopped: customer_id=%s not found", customer_id)
                return response(404, {"message": f"Customer {customer_id} not found. Create the customer before creating an order."})

            logger.info("Customer validated customer_id=%s", customer_id)

            cur.execute(
                """
                INSERT INTO orders
                (customer_id, order_number, status, total_amount)
                VALUES (%s, '', 'PENDING', 0)
                """,
                (customer_id,)
            )
            order_id = cur.lastrowid
            order_number = f"ORD-{order_id:06d}"
            total = Decimal("0.00")
            order_items = []
            logger.info("Order record created order_id=%s order_number=%s", order_id, order_number)

            for index, item in enumerate(data["items"], start=1):
                logger.info("Processing order item number=%s order_id=%s", index, order_id)

                if not isinstance(item, dict):
                    raise ValueError(f"Order item {index} must be a JSON object.")
                if "product_id" not in item:
                    raise ValueError(f"Order item {index} requires product_id.")
                if "quantity" not in item:
                    raise ValueError(f"Order item {index} requires quantity.")

                try:
                    product_id = int(item["product_id"])
                except (TypeError, ValueError):
                    raise ValueError(f"Order item {index} product_id must be a valid integer.")

                try:
                    quantity = int(item["quantity"])
                except (TypeError, ValueError):
                    raise ValueError(f"Order item {index} quantity must be a valid integer.")

                if quantity <= 0:
                    raise ValueError(f"Order item {index} quantity must be greater than 0.")

                logger.info("Checking product and inventory product_id=%s requested_quantity=%s order_id=%s", product_id, quantity, order_id)
                cur.execute(
                    """
                    SELECT
                        p.product_id,
                        p.name,
                        p.price,
                        i.quantity_available
                    FROM products p
                    JOIN inventory i ON p.product_id=i.product_id
                    WHERE p.product_id=%s
                      AND p.is_active=TRUE
                    FOR UPDATE
                    """,
                    (product_id,)
                )
                product = cur.fetchone()

                if not product:
                    raise ValueError(f"Product {product_id} was not found or is inactive, or inventory does not exist.")

                available = int(product["quantity_available"])
                logger.info("Inventory before update product_id=%s available=%s requested=%s order_id=%s", product_id, available, quantity, order_id)

                if quantity > available:
                    raise ValueError(f"Insufficient stock for product '{product['name']}'. Requested {quantity}, but only {available} is available.")

                unit_price = Decimal(str(product["price"]))
                item_total = unit_price * quantity
                total += item_total

                cur.execute(
                    """
                    INSERT INTO order_items
                    (order_id, product_id, quantity, unit_price, total_price)
                    VALUES (%s,%s,%s,%s,%s)
                    """,
                    (order_id, product_id, quantity, unit_price, item_total)
                )
                logger.info("Order item inserted order_id=%s product_id=%s quantity=%s", order_id, product_id, quantity)

                cur.execute(
                    """
                    UPDATE inventory
                    SET quantity_available = quantity_available - %s
                    WHERE product_id=%s
                      AND quantity_available >= %s
                    """,
                    (quantity, product_id, quantity)
                )

                updated_rows = cur.rowcount
                logger.info("Inventory UPDATE executed order_id=%s product_id=%s requested_decrease=%s affected_rows=%s", order_id, product_id, quantity, updated_rows)

                if updated_rows != 1:
                    raise RuntimeError(f"Inventory update failed for product {product_id}. Expected 1 row to change, but {updated_rows} rows changed.")

                cur.execute(
                    """
                    SELECT quantity_available
                    FROM inventory
                    WHERE product_id=%s
                    FOR UPDATE
                    """,
                    (product_id,)
                )
                inventory_after = cur.fetchone()

                if not inventory_after:
                    raise RuntimeError(f"Inventory verification failed for product {product_id}. The inventory row could not be found after the update.")

                after_quantity = int(inventory_after["quantity_available"])
                expected_quantity = available - quantity
                logger.info("Inventory after update order_id=%s product_id=%s before=%s requested_decrease=%s after=%s expected=%s", order_id, product_id, available, quantity, after_quantity, expected_quantity)

                if after_quantity != expected_quantity:
                    raise RuntimeError(f"Inventory verification failed for product {product_id}. Expected quantity {expected_quantity}, but database returned {after_quantity}.")

                order_items.append({
                    "product_id": product_id,
                    "product_name": product["name"],
                    "quantity": quantity,
                    "unit_price": unit_price,
                    "total_price": item_total
                })

                logger.info("Order item completed order_id=%s product_id=%s remaining_stock=%s", order_id, product_id, after_quantity)

            cur.execute(
                """
                UPDATE orders
                SET order_number=%s, total_amount=%s
                WHERE order_id=%s
                """,
                (order_number, total, order_id)
            )
            logger.info("Order total updated order_id=%s total_amount=%s", order_id, total)

            cur.execute(
                """
                INSERT INTO order_logs
                (order_id, event_type, old_status, new_status, message)
                VALUES (%s,%s,%s,%s,%s)
                """,
                (order_id, "CREATED", None, "PENDING", "Order created successfully")
            )
            logger.info("Order creation log inserted order_id=%s", order_id)

        logger.info("Committing order transaction order_id=%s", order_id)
        conn.commit()
        logger.info("Order transaction committed successfully order_id=%s order_number=%s total=%s", order_id, order_number, total)

        try:
            publish_event(
                "Order Created",
                {
                    "order_id": order_id,
                    "order_number": order_number,
                    "customer": {
                        "customer_id": customer["customer_id"],
                        "name": customer["name"],
                        "email": customer["email"]
                    },
                    "status": "PENDING",
                    "items": order_items,
                    "total_amount": total,
                    "message": f"Hello {customer['name']}, your order {order_number} has been created successfully."
                }
            )
            logger.info("Order Created event processing completed order_id=%s", order_id)
        except Exception:
            logger.exception("Order was committed but Order Created event publishing failed order_id=%s", order_id)

        logger.info("Order creation completed successfully order_id=%s", order_id)
        return response(
            201,
            {
                "message": "Order created successfully. Inventory was updated and the transaction was committed.",
                "order_id": order_id,
                "order_number": order_number,
                "status": "PENDING",
                "total_amount": total,
                "items": order_items
            }
        )

    except Exception as e:
        logger.error("Order creation failed order_id=%s customer_id=%s error=%s", order_id, customer_id, str(e))
        conn.rollback()
        logger.info("Order transaction rolled back order_id=%s", order_id)

        try:
            publish_event(
                "Order Failed",
                {
                    "customer_id": customer_id,
                    "order_id": order_id,
                    "status": "FAILED",
                    "message": str(e)
                }
            )
        except Exception:
            logger.exception("Failed to publish Order Failed event order_id=%s", order_id)

        raise




@log_endpoint
def list_order(event):
    conn = db()

    with conn.cursor() as cur:
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
            ORDER BY order_id
            """
        )

        return response(
            200,
            {"orders": cur.fetchall()}
        )


@log_endpoint
def list_customer_orders(event):
    customer_id = id_from_path(event, "Customer")
    conn = db()

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT customer_id
            FROM customers
            WHERE customer_id=%s
            """,
            (customer_id,)
        )

        customer = cur.fetchone()

        if not customer:
            return response(
                404,
                {"message": "Customer not found."}
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

        return response(
            200,
            {"orders": cur.fetchall()}
        )


@log_endpoint
def get_order(event):
    order_id = id_from_path(event, "Order")
    conn = db()

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
                {"message": "Order not found."}
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
        {"order": order}
    )


@log_endpoint
def update_order(event):
    order_id = id_from_path(event, "Order")
    data = body(event)

    if not data.get("status"):
        raise ValueError("status is required.")

    new_status = data["status"].strip().upper()

    if new_status not in VALID_ORDER_STATUSES:
        raise ValueError(
            "Invalid order status. Allowed statuses: "
            + ", ".join(sorted(VALID_ORDER_STATUSES))
        )

    conn = db()

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
                    {"message": "Order not found."}
                )

            old_status = order["status"]

            allowed_transitions = (
                ORDER_STATUS_TRANSITIONS.get(
                    old_status,
                    set()
                )
            )

            if new_status not in allowed_transitions:
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

        publish_event(
            f"Order {new_status.title()}",
            {
                "order_id": order_id,
                "order_number": order["order_number"],
                "customer": {
                    "customer_id": order["customer_id"],
                    "name": order["name"],
                    "email": order["email"]
                },
                "previous_status": old_status,
                "status": new_status,
                "message": (
                    f"Hello {order['name']}, "
                    f"your order {order['order_number']} "
                    f"is now {new_status}."
                )
            }
        )

        return response(
            200,
            {
                "message": "Order updated successfully.",
                "order_id": order_id,
                "order_number": order["order_number"],
                "status": new_status
            }
        )

    except Exception:
        conn.rollback()
        raise


@log_endpoint
def cancel_order(event):
    order_id = id_from_path(event, "Order")
    conn = db()

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
                    {"message": "Order not found."}
                )

            current_status = order["status"]

            if current_status == "CANCELLED":
                return response(
                    400,
                    {"message": "Order is already cancelled."}
                )

            if current_status in ("SHIPPED", "DELIVERED"):
                return response(
                    400,
                    {
                        "message":
                        f"Cannot cancel an order with status "
                        f"{current_status}."
                    }
                )

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
                    "Order cancelled"
                )
            )

        conn.commit()

        publish_event(
            "Order Cancelled",
            {
                "order_id": order_id,
                "order_number": order["order_number"],
                "customer": {
                    "customer_id": order["customer_id"],
                    "name": order["name"],
                    "email": order["email"]
                },
                "previous_status": current_status,
                "status": "CANCELLED",
                "message": (
                    f"Hello {order['name']}, "
                    f"your order {order['order_number']} "
                    f"has been cancelled."
                )
            }
        )

        return response(
            200,
            {
                "message": "Order cancelled successfully.",
                "order_id": order_id,
                "order_number": order["order_number"],
                "status": "CANCELLED"
            }
        )

    except Exception:
        conn.rollback()
        raise


@log_endpoint
def delete_order(event):
    order_id = id_from_path(event, "Order")
    conn = db()

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
                    {"message": "Order not found."}
                )

            old_status = order["status"]

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
                    old_status,
                    "CANCELLED",
                    "Order cancelled"
                )
            )

        conn.commit()

        publish_event(
            "Order Cancelled",
            {
                "order_id": order_id,
                "order_number": order["order_number"],
                "customer": {
                    "customer_id": order["customer_id"],
                    "name": order["name"],
                    "email": order["email"]
                },
                "previous_status": old_status,
                "status": "CANCELLED",
                "message": (
                    f"Hello {order['name']}, "
                    f"your order {order['order_number']} "
                    f"has been cancelled."
                )
            }
        )

        return response(
            200,
            {
                "message": "Order cancelled successfully.",
                "order_id": order_id,
                "order_number": order["order_number"],
                "status": "CANCELLED"
            }
        )

    except Exception:
        conn.rollback()
        raise


@log_endpoint
def lambda_handler(event, context):
    request_id = getattr(context, "aws_request_id", "unknown") if context else "unknown"
    method = event.get("httpMethod", "").upper()
    path = event.get("path", "")
    logger.info("REQUEST received request_id=%s method=%s path=%s", request_id, method, path)

    try:
        if method == "OPTIONS":
            logger.info("OPTIONS request completed request_id=%s", request_id)
            return response(204, {})

        if method == "POST" and path.endswith("/customer"):
            logger.info("Routing request to create_customer request_id=%s", request_id)
            return create_customer(event)

        if method == "GET" and path.endswith("/customer"):
            logger.info("Routing request to list_customers request_id=%s", request_id)
            return list_customers(event)

        if method == "GET" and "/customer/" in path:
            logger.info("Routing request to get_customer request_id=%s", request_id)
            return get_customer(event)

        if method == "PUT" and "/customer/" in path:
            logger.info("Routing request to update_customer request_id=%s", request_id)
            return update_customer(event)

        if method == "DELETE" and "/customer/" in path:
            logger.info("Routing request to delete_customer request_id=%s", request_id)
            return delete_customer(event)

        if method == "POST" and path.endswith("/order"):
            logger.info("Routing request to create_order request_id=%s", request_id)
            return create_order(event)

        if method == "GET" and path.endswith("/order"):
            logger.info("Routing request to list_order request_id=%s", request_id)
            return list_order(event)

        if method == "GET" and "/order/customer/" in path:
            logger.info("Routing request to list_customer_orders request_id=%s", request_id)
            return list_customer_orders(event)

        if method == "POST" and "/order/" in path and path.endswith("/cancel"):
            logger.info("Routing request to cancel_order request_id=%s", request_id)
            return cancel_order(event)

        if method == "GET" and "/order/" in path:
            logger.info("Routing request to get_order request_id=%s", request_id)
            return get_order(event)

        if method == "PUT" and "/order/" in path:
            logger.info("Routing request to update_order request_id=%s", request_id)
            return update_order(event)

        if method == "DELETE" and "/order/" in path:
            logger.info("Routing request to delete_order request_id=%s", request_id)
            return delete_order(event)

        logger.warning("Route not found request_id=%s method=%s path=%s", request_id, method, path)
        return response(404, {"message": f"API route not found for {method} {path}."})

    except ValueError as e:
        logger.warning("Validation error request_id=%s message=%s", request_id, str(e))
        return response(400, {"message": str(e)})

    except pymysql.IntegrityError as e:
        logger.exception("Database integrity error request_id=%s error=%s", request_id, str(e))
        return response(409, {"message": "Database constraint prevented the operation. Check the resource values and try again."})

    except pymysql.MySQLError as e:
        logger.exception("Database error request_id=%s error=%s", request_id, str(e))
        return response(500, {"message": "Database operation failed. Check CloudWatch logs using the request ID for the detailed error.", "request_id": request_id})

    except Exception as e:
        logger.exception("Unhandled API error request_id=%s error=%s", request_id, str(e))
        return response(500, {"message": "The operation failed unexpectedly. Check CloudWatch logs using the request ID for the detailed error.", "request_id": request_id})
