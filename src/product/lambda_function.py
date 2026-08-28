import json
import logging
import os
from decimal import Decimal

import boto3
import pymysql

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ssm = boto3.client("ssm")

ENVIRONMENT = os.environ.get("ENVIRONMENT", "dev")

DB_HOST_PARAMETER = f"/app/{ENVIRONMENT}/database/host"
DB_PORT_PARAMETER = f"/app/{ENVIRONMENT}/database/port"
DB_NAME_PARAMETER = f"/app/{ENVIRONMENT}/database/name"
DB_USERNAME_PARAMETER = f"/app/{ENVIRONMENT}/database/username"
DB_PASSWORD_PARAMETER = f"/app/{ENVIRONMENT}/database/password"

SCHEMA_FILE = os.path.join(os.path.dirname(__file__), "schema.sql")

# Reuse a connection between warm Lambda invocations.
_connection = None


def get_parameter(name):
    response = ssm.get_parameter(
        Name=name,
        WithDecryption=True
    )
    return response["Parameter"]["Value"]


def get_database_config():
    return {
        "host": get_parameter(DB_HOST_PARAMETER),
        "port": int(get_parameter(DB_PORT_PARAMETER)),
        "database": get_parameter(DB_NAME_PARAMETER),
        "username": get_parameter(DB_USERNAME_PARAMETER),
        "password": get_parameter(DB_PASSWORD_PARAMETER),
    }


def connect_to_server(config):
    """Connect to MySQL without selecting a database."""
    return pymysql.connect(
        host=config["host"],
        port=config["port"],
        user=config["username"],
        password=config["password"],
        charset="utf8mb4",
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=5,
        read_timeout=10,
        write_timeout=10,
    )


def connect_to_database(config):
    """Connect to the configured CloudMart database."""
    return pymysql.connect(
        host=config["host"],
        port=config["port"],
        user=config["username"],
        password=config["password"],
        database=config["database"],
        charset="utf8mb4",
        autocommit=False,
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=5,
        read_timeout=10,
        write_timeout=10,
    )


def split_sql_statements(sql):
    """
    Split the simple CREATE DATABASE / CREATE TABLE statements in schema.sql.
    The schema intentionally contains no stored procedures or triggers, so a
    lightweight splitter is sufficient.
    """
    statements = []
    current = []

    for line in sql.splitlines():
        stripped = line.strip()

        if not stripped or stripped.startswith("--"):
            continue

        current.append(line)

        if stripped.endswith(";"):
            statement = "\n".join(current).strip()
            statement = statement[:-1].strip()
            if statement:
                statements.append(statement)
            current = []

    if current:
        statement = "\n".join(current).strip()
        if statement:
            statements.append(statement)

    return statements


def initialize_database_and_tables(config):
    """
    First-time/self-healing initialization.

    1. Connects to the MySQL server without selecting the database.
    2. Creates the CloudMart database if it does not exist.
    3. Creates all tables from schema.sql if they do not exist.
    """
    logger.info("Checking CloudMart database and tables...")

    server_connection = connect_to_server(config)

    try:
        with server_connection.cursor() as cursor:
            cursor.execute(
                "CREATE DATABASE IF NOT EXISTS `%s` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                % config["database"].replace("`", "``")
            )
    finally:
        server_connection.close()

    connection = connect_to_database(config)

    try:
        with open(SCHEMA_FILE, "r", encoding="utf-8") as file:
            schema = file.read()

        # schema.sql includes CREATE DATABASE for standalone/manual use.
        # The Lambda has already selected the database, so execute only
        # statements that apply to the selected database.
        statements = split_sql_statements(schema)

        with connection.cursor() as cursor:
            for statement in statements:
                normalized = statement.lstrip().upper()

                if normalized.startswith("CREATE DATABASE"):
                    continue

                cursor.execute(statement)

        connection.commit()
        logger.info("CloudMart database and tables are ready.")

    except Exception:
        connection.rollback()
        connection.close()
        raise

    return connection


def get_connection():
    global _connection

    config = get_database_config()

    if _connection is not None:
        try:
            _connection.ping(reconnect=True)
            return _connection
        except Exception:
            try:
                _connection.close()
            except Exception:
                pass
            _connection = None

    _connection = initialize_database_and_tables(config)
    return _connection


def response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
            "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS",
        },
        "body": json.dumps(body, default=str),
    }


def get_request_body(event):
    body = event.get("body")

    if body is None:
        return {}

    if isinstance(body, dict):
        return body

    try:
        return json.loads(body)
    except (json.JSONDecodeError, TypeError):
        raise ValueError("Request body must contain valid JSON.")


def get_path_parameters(event):
    return event.get("pathParameters") or {}


def validate_product_input(data, require_all=False):
    required = ["name", "price", "category"]

    if require_all:
        missing = [field for field in required if field not in data]
        if missing:
            raise ValueError(
                "Missing required fields: " + ", ".join(missing)
            )

    if "name" in data and (
        not isinstance(data["name"], str) or not data["name"].strip()
    ):
        raise ValueError("name must be a non-empty string.")

    if "price" in data:
        try:
            price = Decimal(str(data["price"]))
        except Exception:
            raise ValueError("price must be a valid number.")

        if price < 0:
            raise ValueError("price cannot be negative.")

    if "category" in data and (
        not isinstance(data["category"], str) or not data["category"].strip()
    ):
        raise ValueError("category must be a non-empty string.")


def create_product(event):
    data = get_request_body(event)
    validate_product_input(data, require_all=True)

    name = data["name"].strip()
    description = data.get("description")
    price = Decimal(str(data["price"]))
    category = data["category"].strip()

    connection = get_connection()

    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO products
                (name, description, price, category)
            VALUES
                (%s, %s, %s, %s)
            """,
            (name, description, price, category),
        )

        product_id = cursor.lastrowid

        # Create the inventory row when the product is first created.
        quantity_available = int(data.get("quantity_available", 0))
        reorder_threshold = int(data.get("reorder_threshold", 10))

        if quantity_available < 0:
            raise ValueError("quantity_available cannot be negative.")

        if reorder_threshold < 0:
            raise ValueError("reorder_threshold cannot be negative.")

        cursor.execute(
            """
            INSERT INTO inventory
                (product_id, quantity_available, reorder_threshold)
            VALUES
                (%s, %s, %s)
            """,
            (product_id, quantity_available, reorder_threshold),
        )

    connection.commit()

    return response(
        201,
        {
            "message": "Product created successfully.",
            "product_id": product_id,
        },
    )


def list_products(event):
    connection = get_connection()

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                p.product_id,
                p.name,
                p.description,
                p.price,
                p.category,
                p.created_at,
                p.updated_at,
                i.quantity_available,
                i.reorder_threshold,
                i.last_updated_at
            FROM products p
            LEFT JOIN inventory i
                ON p.product_id = i.product_id
            ORDER BY p.product_id
            """
        )

        products = cursor.fetchall()

    return response(200, {"products": products})


def get_product(event):
    path = get_path_parameters(event)
    product_id = path.get("productId")

    if not product_id or not str(product_id).isdigit():
        return response(400, {"message": "productId must be a valid integer."})

    connection = get_connection()

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                p.product_id,
                p.name,
                p.description,
                p.price,
                p.category,
                p.created_at,
                p.updated_at,
                i.quantity_available,
                i.reorder_threshold,
                i.last_updated_at
            FROM products p
            LEFT JOIN inventory i
                ON p.product_id = i.product_id
            WHERE p.product_id = %s
            """,
            (int(product_id),),
        )

        product = cursor.fetchone()

    if product is None:
        return response(404, {"message": "Product not found."})

    return response(200, {"product": product})


def update_product(event):
    path = get_path_parameters(event)
    product_id = path.get("productId")

    if not product_id or not str(product_id).isdigit():
        return response(400, {"message": "productId must be a valid integer."})

    data = get_request_body(event)
    validate_product_input(data, require_all=False)

    connection = get_connection()

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT product_id FROM products WHERE product_id = %s",
            (int(product_id),),
        )

        if cursor.fetchone() is None:
            return response(404, {"message": "Product not found."})

        product_fields = []
        product_values = []

        for field in ["name", "description", "price", "category"]:
            if field in data:
                value = data[field]

                if field == "name" or field == "category":
                    value = value.strip()

                if field == "price":
                    value = Decimal(str(value))

                product_fields.append(f"{field} = %s")
                product_values.append(value)

        if product_fields:
            product_values.append(int(product_id))

            cursor.execute(
                f"""
                UPDATE products
                SET {", ".join(product_fields)}
                WHERE product_id = %s
                """,
                product_values,
            )

        inventory_fields = []
        inventory_values = []

        if "quantity_available" in data:
            quantity = int(data["quantity_available"])
            if quantity < 0:
                raise ValueError("quantity_available cannot be negative.")

            inventory_fields.append("quantity_available = %s")
            inventory_values.append(quantity)

        if "reorder_threshold" in data:
            threshold = int(data["reorder_threshold"])
            if threshold < 0:
                raise ValueError("reorder_threshold cannot be negative.")

            inventory_fields.append("reorder_threshold = %s")
            inventory_values.append(threshold)

        if inventory_fields:
            inventory_values.append(int(product_id))

            cursor.execute(
                f"""
                INSERT INTO inventory
                    (product_id, quantity_available, reorder_threshold)
                VALUES
                    (%s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    {", ".join(
                        field.replace(" = %s", " = VALUES(" + field.split(" = ")[0] + ")")
                        for field in inventory_fields
                    )}
                """,
                [int(product_id)] + inventory_values[:-1],
            )

    connection.commit()

    return response(
        200,
        {
            "message": "Product updated successfully.",
            "product_id": int(product_id),
        },
    )


def delete_product(event):
    path = get_path_parameters(event)
    product_id = path.get("productId")

    if not product_id or not str(product_id).isdigit():
        return response(400, {"message": "productId must be a valid integer."})

    connection = get_connection()

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT product_id FROM products WHERE product_id = %s",
            (int(product_id),),
        )

        if cursor.fetchone() is None:
            return response(404, {"message": "Product not found."})

        cursor.execute(
            "DELETE FROM products WHERE product_id = %s",
            (int(product_id),),
        )

    connection.commit()

    return response(
        200,
        {
            "message": "Product deleted successfully.",
            "product_id": int(product_id),
        },
    )


def lambda_handler(event, context):
    """
    CloudMart Product API Lambda.

    The first invocation, and every later cold-start, checks that the
    CloudMart database and ER-diagram tables exist. Missing database/tables
    are created automatically from schema.sql.
    """

    logger.info(
        "Product API request: method=%s path=%s",
        event.get("httpMethod"),
        event.get("path"),
    )

    method = (event.get("httpMethod") or "").upper()

    if method == "OPTIONS":
        return response(204, {})

    try:
        # This ensures the database/tables exist before Product API work.
        get_connection()

        path = event.get("path", "")

        if method == "POST" and path.endswith("/products"):
            return create_product(event)

        if method == "GET" and path.endswith("/products"):
            return list_products(event)

        if method == "GET" and "/products/" in path:
            return get_product(event)

        if method == "PUT" and "/products/" in path:
            return update_product(event)

        if method == "DELETE" and "/products/" in path:
            return delete_product(event)

        return response(404, {"message": "Product API route not found."})

    except ValueError as error:
        logger.warning("Validation error: %s", error)
        return response(400, {"message": str(error)})

    except pymysql.MySQLError as error:
        logger.exception("Database error")
        try:
            if _connection is not None:
                _connection.rollback()
        except Exception:
            pass

        return response(
            500,
            {
                "message": "Database operation failed.",
                "error": str(error),
            },
        )

    except Exception as error:
        logger.exception("Product API error")
        return response(
            500,
            {
                "message": "Internal server error.",
                "error": str(error),
            },
        )
