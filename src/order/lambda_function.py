import json
import logging
import os
from decimal import Decimal

import boto3
import pymysql

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ssm = boto3.client("ssm")

ENV = os.getenv("ENVIRONMENT", "dev")

_connection = None


def db():
    global _connection

    if _connection:
        try:
            _connection.ping(reconnect=True)
            return _connection
        except Exception:
            _connection = None

    names = [
        f"/app/{ENV}/database/host",
        f"/app/{ENV}/database/port",
        f"/app/{ENV}/database/name",
        f"/app/{ENV}/database/username",
        f"/app/{ENV}/database/password"
    ]

    params = ssm.get_parameters(
        Names=names,
        WithDecryption=True
    )["Parameters"]

    p = {
        x["Name"].split("/")[-1]: x["Value"]
        for x in params
    }

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

    return _connection


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


def order_id(event):
    value = (event.get("pathParameters") or {}).get("id")

    if not value or not str(value).isdigit():
        raise ValueError("Order ID must be a valid integer.")

    return int(value)


def create_order(event):
    data = body(event)

    if "customer_id" not in data:
        raise ValueError("customer_id is required.")

    if "items" not in data or not data["items"]:
        raise ValueError("items are required.")

    customer_id = int(data["customer_id"])
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
                INSERT INTO orders
                (customer_id, order_number, status, total_amount)
                VALUES (%s, '', 'PENDING', 0)
                """,
                (customer_id,)
            )

            oid = cur.lastrowid
            order_number = f"ORD-{oid:06d}"
            total = Decimal("0.00")

            for item in data["items"]:

                if "product_id" not in item or "quantity" not in item:
                    raise ValueError(
                        "Each item requires product_id and quantity."
                    )

                product_id = int(item["product_id"])
                quantity = int(item["quantity"])

                if quantity <= 0:
                    raise ValueError(
                        "Quantity must be greater than 0."
                    )

                cur.execute(
                    """
                    SELECT p.price,
                           i.quantity_available
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
                        f"Product {product_id} not found."
                    )

                available = int(product["quantity_available"])

                if quantity > available:
                    raise ValueError(
                        f"Insufficient stock for product {product_id}."
                    )

                price = Decimal(str(product["price"]))
                item_total = price * quantity
                total += item_total

                cur.execute(
                    """
                    INSERT INTO order_items
                    (order_id, product_id, quantity,
                     unit_price, total_price)
                    VALUES (%s,%s,%s,%s,%s)
                    """,
                    (
                        oid,
                        product_id,
                        quantity,
                        price,
                        item_total
                    )
                )

                cur.execute(
                    """
                    UPDATE inventory
                    SET quantity_available =
                        quantity_available - %s
                    WHERE product_id=%s
                    """,
                    (quantity, product_id)
                )

            cur.execute(
                """
                UPDATE orders
                SET order_number=%s,
                    total_amount=%s
                WHERE order_id=%s
                """,
                (
                    order_number,
                    total,
                    oid
                )
            )

            cur.execute(
                """
                INSERT INTO order_logs
                (order_id, event_type, old_status,
                 new_status, message)
                VALUES (%s,%s,%s,%s,%s)
                """,
                (
                    oid,
                    "CREATED",
                    None,
                    "PENDING",
                    "Order created"
                )
            )

        conn.commit()

        return response(
            201,
            {
                "message": "Order created successfully.",
                "order_id": oid,
                "order_number": order_number,
                "status": "PENDING",
                "total_amount": total
            }
        )

    except Exception:
        conn.rollback()
        raise


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


def get_order(event):
    oid = order_id(event)
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
            WHERE order_id=%s
            """,
            (oid,)
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
                order_item_id,
                product_id,
                quantity,
                unit_price,
                total_price
            FROM order_items
            WHERE order_id=%s
            ORDER BY order_item_id
            """,
            (oid,)
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
            (oid,)
        )

        order["logs"] = cur.fetchall()

    return response(
        200,
        {"order": order}
    )


def update_order(event):
    oid = order_id(event)
    data = body(event)

    if "status" not in data:
        raise ValueError("status is required.")

    new_status = data["status"].strip()

    if not new_status:
        raise ValueError("status cannot be empty.")

    conn = db()

    try:
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT status
                FROM orders
                WHERE order_id=%s
                FOR UPDATE
                """,
                (oid,)
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
                SET status=%s
                WHERE order_id=%s
                """,
                (
                    new_status,
                    oid
                )
            )

            cur.execute(
                """
                INSERT INTO order_logs
                (order_id, event_type, old_status,
                 new_status, message)
                VALUES (%s,%s,%s,%s,%s)
                """,
                (
                    oid,
                    "STATUS_CHANGED",
                    old_status,
                    new_status,
                    f"Order status changed from "
                    f"{old_status} to {new_status}"
                )
            )

        conn.commit()

        return response(
            200,
            {
                "message": "Order updated successfully.",
                "order_id": oid,
                "status": new_status
            }
        )

    except Exception:
        conn.rollback()
        raise


def delete_order(event):
    oid = order_id(event)
    conn = db()

    try:
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT status
                FROM orders
                WHERE order_id=%s
                FOR UPDATE
                """,
                (oid,)
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
                (oid,)
            )

            cur.execute(
                """
                INSERT INTO order_logs
                (order_id, event_type, old_status,
                 new_status, message)
                VALUES (%s,%s,%s,%s,%s)
                """,
                (
                    oid,
                    "CANCELLED",
                    old_status,
                    "CANCELLED",
                    "Order cancelled"
                )
            )

        conn.commit()

        return response(
            200,
            {
                "message": "Order cancelled successfully.",
                "order_id": oid,
                "status": "CANCELLED"
            }
        )

    except Exception:
        conn.rollback()
        raise


def lambda_handler(event, context):

    try:
        method = event.get("httpMethod", "").upper()
        path = event.get("path", "")

        if method == "OPTIONS":
            return response(204, {})

        if method == "POST" and path.endswith("/order"):
            return create_order(event)

        if method == "GET" and path.endswith("/order"):
            return list_order(event)

        if method == "GET" and "/order/" in path:
            return get_order(event)

        if method == "PUT" and "/order/" in path:
            return update_order(event)

        if method == "DELETE" and "/order/" in path:
            return delete_order(event)

        return response(
            404,
            {"message": "Order API route not found."}
        )

    except ValueError as e:
        return response(
            400,
            {"message": str(e)}
        )

    except pymysql.MySQLError as e:
        logger.exception("Database error")

        return response(
            500,
            {
                "message": "Database operation failed.",
                "error": str(e)
            }
        )

    except Exception as e:
        logger.exception("Order API error")

        return response(
            500,
            {
                "message": "Internal server error.",
                "error": str(e)
            }
        )