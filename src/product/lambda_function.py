import json
import logging
import os
from decimal import Decimal

import boto3
import pymysql

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ssm = boto3.client("ssm")
events = boto3.client("event")
ENV = os.getenv("ENVIRONMENT", "dev")
SCHEMA = os.path.join(os.path.dirname(__file__), "schema.sql")

PARAMS = {
    "host": f"/app/{ENV}/database/host",
    "port": f"/app/{ENV}/database/port",
    "database": f"/app/{ENV}/database/name",
    "username": f"/app/{ENV}/database/username",
    "password": f"/app/{ENV}/database/password",
}

_connection = None


def parameter(name):
    return ssm.get_parameter(
        Name=PARAMS[name],
        WithDecryption=True
    )["Parameter"]["Value"]


def config():
    return {
        "host": parameter("host"),
        "port": int(parameter("port")),
        "database": parameter("database"),
        "user": parameter("username"),
        "password": parameter("password")
    }


def connect_server(c):
    return pymysql.connect(
        host=c["host"],
        port=c["port"],
        user=c["user"],
        password=c["password"],
        charset="utf8mb4",
        autocommit=True,
        connect_timeout=5
    )


def connect_db(c):
    return pymysql.connect(
        host=c["host"],
        port=c["port"],
        user=c["user"],
        password=c["password"],
        database=c["database"],
        charset="utf8mb4",
        autocommit=False,
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=5
    )


def initialize_database(c):
    # Create database if it does not exist
    server = connect_server(c)

    try:
        with server.cursor() as cur:
            db = c["database"].replace("`", "``")
            cur.execute(f"CREATE DATABASE IF NOT EXISTS `{db}`")
    finally:
        server.close()

    # Connect to the database
    conn = connect_db(c)

    # Execute schema.sql
    with open(SCHEMA, "r", encoding="utf-8") as f:
        sql = f.read()

    try:
        with conn.cursor() as cur:
            for statement in sql.split(";"):
                statement = statement.strip()

                if statement and not statement.startswith("--"):
                    cur.execute(statement)

        conn.commit()
        logger.info("Database and tables are ready.")
        return conn

    except Exception:
        conn.rollback()
        conn.close()
        raise


def db():
    global _connection

    c = config()

    if _connection:
        try:
            _connection.ping(reconnect=True)
            return _connection
        except Exception:
            try:
                _connection.close()
            except Exception:
                pass

    _connection = initialize_database(c)
    return _connection


def response(status, body):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
            "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS"
        },
        "body": json.dumps(body, default=str)
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
        raise ValueError("Request body must contain valid JSON.")


def product_id(event):
    value = (event.get("pathParameters") or {}).get("id")

    if not value or not str(value).isdigit():
        raise ValueError("Product ID must be a valid integer.")

    return int(value)


def validate(data, create=False):
    if create:
        for field in ("name", "price", "category"):
            if field not in data:
                raise ValueError(f"Missing required field: {field}")

    if "name" in data:
        if not isinstance(data["name"], str) or not data["name"].strip():
            raise ValueError("name must be a non-empty string.")

    if "category" in data:
        if not isinstance(data["category"], str) or not data["category"].strip():
            raise ValueError("category must be a non-empty string.")

    if "price" in data:
        try:
            price = Decimal(str(data["price"]))
            if price < 0:
                raise ValueError
        except Exception:
            raise ValueError("price must be a valid non-negative number.")

    for field in ("quantity_available", "reorder_threshold"):
        if field in data:
            try:
                if int(data[field]) < 0:
                    raise ValueError
            except Exception:
                raise ValueError(f"{field} must be a non-negative integer.")


def create_product(event):
    data = body(event)
    validate(data, True)

    conn = db()

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO products
                    (name, description, price, category, is_active)
                VALUES
                    (%s, %s, %s, %s, TRUE)
                """,
                (
                    data["name"].strip(),
                    data.get("description"),
                    Decimal(str(data["price"])),
                    data["category"].strip()
                )
            )

            pid = cur.lastrowid

            cur.execute(
                """
                INSERT INTO inventory
                    (product_id, quantity_available, reorder_threshold)
                VALUES
                    (%s, %s, %s)
                """,
                (
                    pid,
                    int(data.get("quantity_available", 0)),
                    int(data.get("reorder_threshold", 10))
                )
            )

        conn.commit()

        return response(
            201,
            {
                "message": "Product created successfully.",
                "product_id": pid
            }
        )

    except Exception:
        conn.rollback()
        raise


def list_products(event):
    conn = db()

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                p.product_id,
                p.name,
                p.description,
                p.price,
                p.category,
                p.is_active,
                p.created_at,
                p.updated_at,
                i.quantity_available,
                i.reorder_threshold,
                i.last_updated_at
            FROM products p
            LEFT JOIN inventory i
                ON p.product_id = i.product_id
            WHERE p.is_active = TRUE
            ORDER BY p.product_id
            """
        )

        return response(200, {"products": cur.fetchall()})


def get_product(event):
    pid = product_id(event)
    conn = db()

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                p.product_id,
                p.name,
                p.description,
                p.price,
                p.category,
                p.is_active,
                p.created_at,
                p.updated_at,
                i.quantity_available,
                i.reorder_threshold,
                i.last_updated_at
            FROM products p
            LEFT JOIN inventory i
                ON p.product_id = i.product_id
            WHERE p.product_id = %s
              AND p.is_active = TRUE
            """,
            (pid,)
        )

        product = cur.fetchone()

    if not product:
        return response(404, {"message": "Product not found."})

    return response(200, {"product": product})


def update_product(event):
    pid = product_id(event)
    data = body(event)

    if not data:
        raise ValueError("Request body cannot be empty.")

    validate(data)

    conn = db()

    try:
        with conn.cursor() as cur:

            cur.execute(
                "SELECT product_id FROM products WHERE product_id = %s",
                (pid,)
            )

            if not cur.fetchone():
                return response(404, {"message": "Product not found."})

            fields = []
            values = []

            for field in ("name", "description", "price", "category"):
                if field in data:
                    value = data[field]

                    if field in ("name", "category"):
                        value = value.strip()

                    if field == "price":
                        value = Decimal(str(value))

                    fields.append(f"{field} = %s")
                    values.append(value)

            if fields:
                values.append(pid)

                cur.execute(
                    f"""
                    UPDATE products
                    SET {", ".join(fields)}
                    WHERE product_id = %s
                    """,
                    values
                )

            inventory = []

            if "quantity_available" in data:
                inventory.append(
                    ("quantity_available", int(data["quantity_available"]))
                )

            if "reorder_threshold" in data:
                inventory.append(
                    ("reorder_threshold", int(data["reorder_threshold"]))
                )

            if inventory:
                cur.execute(
                    """
                    INSERT INTO inventory
                        (product_id, quantity_available, reorder_threshold)
                    VALUES
                        (%s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        quantity_available = VALUES(quantity_available),
                        reorder_threshold = VALUES(reorder_threshold)
                    """,
                    (
                        pid,
                        int(data.get("quantity_available", 0)),
                        int(data.get("reorder_threshold", 10))
                    )
                )
        quantity = int(data.get("quantity_available", 0))
        threshold = int(data.get("reorder_threshold", 10))

        if "quantity_available" in data:
            publish_inventory_event(
                pid,
                quantity,
                threshold
            )
        conn.commit()

        return response(
            200,
            {
                "message": "Product updated successfully.",
                "product_id": pid
            }
        )

    except Exception:
        conn.rollback()
        raise


def delete_product(event):
    pid = product_id(event)
    conn = db()

    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT product_id FROM products WHERE product_id = %s",
                (pid,)
            )

            if not cur.fetchone():
                return response(404, {"message": "Product not found."})

            # Soft delete
            cur.execute(
                """
                UPDATE products
                SET is_active = FALSE
                WHERE product_id = %s
                """,
                (pid,)
            )

        conn.commit()

        return response(
            200,
            {
                "message": "Product deactivated successfully.",
                "product_id": pid,
                "is_active": False
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

        # Database + schema check happens before API operation
        db()

        if method == "POST" and path.endswith("/product"):
            return create_product(event)

        if method == "GET" and path.endswith("/product"):
            return list_products(event)

        if method == "GET" and "/product/" in path:
            return get_product(event)

        if method == "PUT" and "/product/" in path:
            return update_product(event)

        if method == "DELETE" and "/product/" in path:
            return delete_product(event)

        return response(404, {"message": "Product API route not found."})

    except ValueError as e:
        return response(400, {"message": str(e)})

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
        logger.exception("Product API error")
        return response(
            500,
            {
                "message": "Internal server error.",
                "error": str(e)
            }
        )
def publish_inventory_event(product_id, quantity, threshold):
    if quantity >= threshold:
        return

    events.put_events(
        Entries=[
            {
                "EventBusName": EVENT_BUS,
                "Source": "cloudmart.product",
                "DetailType": "Low Stock Alert",
                "Detail": json.dumps({
                    "product_id": product_id,
                    "quantity_available": quantity,
                    "reorder_threshold": threshold
                })
            }
        ]
    )