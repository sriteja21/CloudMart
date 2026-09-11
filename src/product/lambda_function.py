import json
import logging
import os
from decimal import Decimal

import boto3
import pymysql

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ssm = boto3.client("ssm")
events = boto3.client("events")

ENV = os.getenv("ENVIRONMENT", "dev")
EVENT_BUS = f"cloudmart-{ENV}-event-bus"
SCHEMA = os.path.join(os.path.dirname(__file__), "schema.sql")

PARAMS = {
    "host": f"/app/{ENV}/database/host",
    "port": f"/app/{ENV}/database/port",
    "database": f"/app/{ENV}/database/name",
    "username": f"/app/{ENV}/database/username",
    "password": f"/app/{ENV}/database/password",
}



def parameter(name):
    logger.info("Reading database parameter: %s", name)
    try:
        value = ssm.get_parameter(
            Name=PARAMS[name],
            WithDecryption=True
        )["Parameter"]["Value"]
        logger.info("Database parameter loaded successfully: %s", name)
        return value
    except Exception:
        logger.exception("Failed to read database parameter: %s", name)
        raise


def config():
    logger.info("Loading database configuration")
    configuration = {
        "host": parameter("host"),
        "port": int(parameter("port")),
        "database": parameter("database"),
        "user": parameter("username"),
        "password": parameter("password")
    }
    logger.info(
        "Database configuration loaded successfully: host=%s port=%s database=%s user=%s",
        configuration["host"],
        configuration["port"],
        configuration["database"],
        configuration["user"]
    )
    return configuration


def connect_server(c):
    logger.info("Connecting to MySQL server")
    connection = pymysql.connect(
        host=c["host"],
        port=c["port"],
        user=c["user"],
        password=c["password"],
        charset="utf8mb4",
        autocommit=True,
        connect_timeout=5
    )
    logger.info("MySQL server connection established")
    return connection


def connect_db(c):
    logger.info("Connecting to MySQL database: %s", c["database"])
    connection = pymysql.connect(
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
    logger.info("MySQL database connection established")
    return connection


def initialize_database(c):
    logger.info("Database initialization started")
    server = connect_server(c)

    try:
        with server.cursor() as cur:
            db_name = c["database"].replace("`", "``")
            logger.info("Checking database exists: %s", c["database"])
            cur.execute(f"CREATE DATABASE IF NOT EXISTS `{db_name}`")
            logger.info("Database exists or was created successfully")
    finally:
        server.close()
        logger.info("MySQL server connection closed")

    conn = connect_db(c)

    logger.info("Reading schema file: %s", SCHEMA)
    with open(SCHEMA, "r", encoding="utf-8") as f:
        sql = f.read()

    try:
        with conn.cursor() as cur:
            statements = sql.split(";")
            logger.info("Schema contains %s SQL statements", len(statements))

            executed = 0
            for statement in statements:
                statement = statement.strip()

                if statement and not statement.startswith("--"):
                    cur.execute(statement)
                    executed += 1

            logger.info("Schema execution completed: statements=%s", executed)

        conn.commit()
        logger.info("Database transaction committed: database initialization")
        logger.info("Database and tables are ready")
        return conn

    except Exception:
        conn.rollback()
        logger.exception("Database initialization failed; transaction rolled back")
        conn.close()
        raise


def db():
    logger.info("Creating new database connection")
    c = config()
    connection = initialize_database(c)
    logger.info("New database connection ready")
    return connection


def response(status, body):
    logger.info("Preparing API response: status=%s", status)

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
    logger.info("Parsing request body")
    data = event.get("body")

    if not data:
        logger.info("Request body is empty")
        return {}

    if isinstance(data, dict):
        logger.info("Request body received as dictionary")
        return data

    try:
        parsed = json.loads(data)
        logger.info("Request body JSON parsed successfully")
        return parsed
    except Exception:
        logger.error("Request body contains invalid JSON")
        raise ValueError("Request body must contain valid JSON.")


def product_id(event):
    value = (event.get("pathParameters") or {}).get("id")

    if not value or not str(value).isdigit():
        logger.error("Invalid product ID received: %s", value)
        raise ValueError("Product ID must be a valid integer.")

    pid = int(value)
    logger.info("Product ID validated: product_id=%s", pid)
    return pid


def validate(data, create=False):
    logger.info("Product request validation started: create=%s", create)

    if create:
        for field in ("name", "price", "category"):
            if field not in data:
                logger.error("Missing required field: %s", field)
                raise ValueError(f"Missing required field: {field}")

    if "name" in data:
        if not isinstance(data["name"], str) or not data["name"].strip():
            logger.error("Invalid name supplied")
            raise ValueError("name must be a non-empty string.")

    if "category" in data:
        if not isinstance(data["category"], str) or not data["category"].strip():
            logger.error("Invalid category supplied")
            raise ValueError("category must be a non-empty string.")

    if "price" in data:
        try:
            price = Decimal(str(data["price"]))
            if price < 0:
                raise ValueError
        except Exception:
            logger.error("Invalid price supplied")
            raise ValueError("price must be a valid non-negative number.")

    for field in ("quantity_available", "reorder_threshold"):
        if field in data:
            try:
                if int(data[field]) < 0:
                    raise ValueError
            except Exception:
                logger.error("Invalid inventory value: field=%s", field)
                raise ValueError(f"{field} must be a non-negative integer.")

    logger.info("Product request validation completed successfully")


def create_product(event):
    logger.info("Product creation started")

    data = body(event)
    validate(data, True)

    conn = db()

    try:
        with conn.cursor() as cur:
            logger.info("Inserting product: name=%s category=%s", data["name"], data["category"])

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
            logger.info("Product inserted successfully: product_id=%s", pid)

            quantity = int(data.get("quantity_available", 0))
            threshold = int(data.get("reorder_threshold", 10))

            logger.info(
                "Creating inventory: product_id=%s quantity_available=%s reorder_threshold=%s",
                pid,
                quantity,
                threshold
            )

            cur.execute(
                """
                INSERT INTO inventory
                    (product_id, quantity_available, reorder_threshold)
                VALUES
                    (%s, %s, %s)
                """,
                (pid, quantity, threshold)
            )

            logger.info("Inventory record created successfully: product_id=%s", pid)

        logger.info("Committing product creation transaction")
        conn.commit()
        logger.info("Product creation transaction committed successfully")

        if quantity < threshold:
            logger.info(
                "Product created below reorder threshold; publishing low stock event: product_id=%s quantity=%s threshold=%s",
                pid,
                quantity,
                threshold
            )
            publish_inventory_event(pid, quantity, threshold)

        logger.info("Product creation completed successfully: product_id=%s", pid)

        return response(
            201,
            {
                "message": "Product created successfully.",
                "product_id": pid
            }
        )

    except pymysql.IntegrityError as e:
        conn.rollback()
        logger.exception(
            "Product creation failed because of database integrity error: %s",
            str(e)
        )
        return response(
            409,
            {
                "message": "Product could not be created because the product data conflicts with an existing record.",
                "error": str(e)
            }
        )

    except Exception:
        conn.rollback()
        logger.exception(
            "Product creation failed; transaction rolled back"
        )
        raise


def list_products(event):
    logger.info("Product list request started")

    conn = db()

    with conn.cursor() as cur:
        logger.info("Querying active products")
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

        products = cur.fetchall()
        logger.info("Active products retrieved: count=%s", len(products))

        return response(200, {"products": products})


def get_product(event):
    pid = product_id(event)
    logger.info("Product retrieval started: product_id=%s", pid)

    conn = db()

    with conn.cursor() as cur:
        logger.info("Querying product and inventory: product_id=%s", pid)

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
        logger.warning("Product not found: product_id=%s", pid)
        return response(404, {"message": f"Product {pid} not found."})

    logger.info(
        "Product retrieved successfully: product_id=%s quantity_available=%s",
        pid,
        product.get("quantity_available")
    )

    return response(200, {"product": product})


def update_product(event):
    pid = product_id(event)
    logger.info("Product update started: product_id=%s", pid)

    data = body(event)

    if not data:
        logger.error("Product update request body is empty: product_id=%s", pid)
        raise ValueError("Request body cannot be empty.")

    validate(data)

    conn = db()

    try:
        with conn.cursor() as cur:
            logger.info("Checking product exists: product_id=%s", pid)

            cur.execute(
                "SELECT product_id FROM products WHERE product_id = %s",
                (pid,)
            )

            if not cur.fetchone():
                logger.warning("Product not found for update: product_id=%s", pid)
                return response(
                    404,
                    {"message": f"Product {pid} not found."}
                )

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
                logger.info(
                    "Updating product fields: product_id=%s fields=%s",
                    pid,
                    ",".join(field.split(" = ")[0] for field in fields)
                )

                values.append(pid)

                cur.execute(
                    f"""
                    UPDATE products
                    SET {", ".join(fields)}
                    WHERE product_id = %s
                    """,
                    values
                )

                logger.info(
                    "Product fields update executed: product_id=%s affected_rows=%s",
                    pid,
                    cur.rowcount
                )
            else:
                logger.info("No product table fields requested for update: product_id=%s", pid)

            inventory_requested = (
                "quantity_available" in data
                or "reorder_threshold" in data
            )

            quantity = None
            threshold = None

            if inventory_requested:
                logger.info("Inventory update requested: product_id=%s", pid)

                cur.execute(
                    """
                    SELECT
                        quantity_available,
                        reorder_threshold
                    FROM inventory
                    WHERE product_id = %s
                    FOR UPDATE
                    """,
                    (pid,)
                )

                current_inventory = cur.fetchone()

                if current_inventory:
                    old_quantity = int(current_inventory["quantity_available"])
                    old_threshold = int(current_inventory["reorder_threshold"])
                else:
                    old_quantity = 0
                    old_threshold = 10

                quantity = int(
                    data["quantity_available"]
                    if "quantity_available" in data
                    else old_quantity
                )

                threshold = int(
                    data["reorder_threshold"]
                    if "reorder_threshold" in data
                    else old_threshold
                )

                logger.info(
                    "Inventory before update: product_id=%s quantity_available=%s reorder_threshold=%s",
                    pid,
                    old_quantity,
                    old_threshold
                )

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
                    (pid, quantity, threshold)
                )

                logger.info(
                    "Inventory update executed: product_id=%s new_quantity=%s new_threshold=%s affected_rows=%s",
                    pid,
                    quantity,
                    threshold,
                    cur.rowcount
                )

                cur.execute(
                    """
                    SELECT
                        quantity_available,
                        reorder_threshold,
                        last_updated_at
                    FROM inventory
                    WHERE product_id = %s
                    """,
                    (pid,)
                )

                verified_inventory = cur.fetchone()

                if not verified_inventory:
                    logger.error(
                        "Inventory verification failed: product_id=%s",
                        pid
                    )
                    raise ValueError(
                        f"Inventory record could not be verified for product {pid}."
                    )

                verified_quantity = int(verified_inventory["quantity_available"])
                verified_threshold = int(verified_inventory["reorder_threshold"])

                logger.info(
                    "Inventory after update: product_id=%s quantity_available=%s reorder_threshold=%s",
                    pid,
                    verified_quantity,
                    verified_threshold
                )

                if verified_quantity != quantity:
                    logger.error(
                        "Inventory quantity verification mismatch: product_id=%s expected=%s actual=%s",
                        pid,
                        quantity,
                        verified_quantity
                    )
                    raise ValueError(
                        f"Inventory quantity update failed for product {pid}. Expected {quantity}, but database contains {verified_quantity}."
                    )

                if verified_threshold != threshold:
                    logger.error(
                        "Inventory threshold verification mismatch: product_id=%s expected=%s actual=%s",
                        pid,
                        threshold,
                        verified_threshold
                    )
                    raise ValueError(
                        f"Inventory reorder threshold update failed for product {pid}. Expected {threshold}, but database contains {verified_threshold}."
                    )

                logger.info(
                    "Inventory verification successful: product_id=%s",
                    pid
                )

        logger.info("Database transaction changes prepared: product_id=%s", pid)

        if inventory_requested and quantity < threshold:
            logger.info(
                "Product is below reorder threshold: product_id=%s quantity=%s threshold=%s",
                pid,
                quantity,
                threshold
            )

        logger.info("Committing product update transaction: product_id=%s", pid)
        conn.commit()
        logger.info("Product update transaction committed successfully: product_id=%s", pid)

        if inventory_requested and quantity < threshold:
            publish_inventory_event(pid, quantity, threshold)

        logger.info("Product update completed successfully: product_id=%s", pid)

        return response(
            200,
            {
                "message": "Product updated successfully.",
                "product_id": pid
            }
        )

    except pymysql.IntegrityError as e:
        conn.rollback()
        logger.exception(
            "Product update failed because of database integrity error: product_id=%s error=%s",
            pid,
            str(e)
        )
        return response(
            409,
            {
                "message": f"Product {pid} could not be updated because the data conflicts with an existing record.",
                "error": str(e)
            }
        )

    except Exception:
        conn.rollback()
        logger.exception(
            "Product update failed; transaction rolled back: product_id=%s",
            pid
        )
        raise


def delete_product(event):
    pid = product_id(event)
    logger.info("Product deactivation started: product_id=%s", pid)

    conn = db()

    try:
        with conn.cursor() as cur:
            logger.info("Checking product exists before deactivation: product_id=%s", pid)

            cur.execute(
                "SELECT product_id FROM products WHERE product_id = %s",
                (pid,)
            )

            if not cur.fetchone():
                logger.warning("Product not found for deactivation: product_id=%s", pid)
                return response(
                    404,
                    {"message": f"Product {pid} not found."}
                )

            cur.execute(
                """
                UPDATE products
                SET is_active = FALSE
                WHERE product_id = %s
                """,
                (pid,)
            )

            logger.info(
                "Product deactivation update executed: product_id=%s affected_rows=%s",
                pid,
                cur.rowcount
            )

        logger.info("Committing product deactivation: product_id=%s", pid)
        conn.commit()
        logger.info("Product deactivation committed successfully: product_id=%s", pid)

        return response(
            200,
            {
                "message": "Product deactivated successfully.",
                "product_id": pid,
                "is_active": False
            }
        )

    except pymysql.IntegrityError as e:
        conn.rollback()
        logger.exception(
            "Product deactivation failed because of database integrity error: product_id=%s error=%s",
            pid,
            str(e)
        )
        return response(
            409,
            {
                "message": f"Product {pid} could not be deactivated because of a database constraint.",
                "error": str(e)
            }
        )

    except Exception:
        conn.rollback()
        logger.exception(
            "Product deactivation failed; transaction rolled back: product_id=%s",
            pid
        )
        raise


def publish_inventory_event(product_id, quantity, threshold):
    logger.info(
        "Publishing low stock event: product_id=%s quantity=%s threshold=%s event_bus=%s",
        product_id,
        quantity,
        threshold,
        EVENT_BUS
    )

    try:
        result = events.put_events(
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

        failed_count = result.get("FailedEntryCount", 0)

        if failed_count > 0:
            logger.error(
                "Low stock EventBridge event failed: product_id=%s failed_entry_count=%s response=%s",
                product_id,
                failed_count,
                result
            )
            raise RuntimeError(
                f"Low stock event could not be published for product {product_id}."
            )

        logger.info(
            "Low stock EventBridge event published successfully: product_id=%s",
            product_id
        )

        return result

    except Exception:
        logger.exception(
            "Failed to publish low stock EventBridge event: product_id=%s",
            product_id
        )
        raise


def lambda_handler(event, context):
    request_id = getattr(context, "aws_request_id", "unknown")
    method = event.get("httpMethod", "").upper()
    path = event.get("path", "")

    logger.info(
        "========== PRODUCT LAMBDA REQUEST START =========="
    )
    logger.info(
        "Request received: request_id=%s method=%s path=%s",
        request_id,
        method,
        path
    )

    try:
        if method == "OPTIONS":
            logger.info("OPTIONS request received")
            result = response(204, {})
            logger.info(
                "========== PRODUCT LAMBDA REQUEST END: status=204 =========="
            )
            return result

        logger.info("Checking database availability before API operation")
        db()
        logger.info("Database availability check successful")

        if method == "POST" and path.endswith("/product"):
            logger.info("Route matched: POST /product")
            result = create_product(event)

        elif method == "GET" and path.endswith("/product"):
            logger.info("Route matched: GET /product")
            result = list_products(event)

        elif method == "GET" and "/product/" in path:
            logger.info("Route matched: GET /product/{id}")
            result = get_product(event)

        elif method == "PUT" and "/product/" in path:
            logger.info("Route matched: PUT /product/{id}")
            result = update_product(event)

        elif method == "DELETE" and "/product/" in path:
            logger.info("Route matched: DELETE /product/{id}")
            result = delete_product(event)

        else:
            logger.warning(
                "Product API route not found: method=%s path=%s",
                method,
                path
            )
            result = response(
                404,
                {"message": "Product API route not found."}
            )

        logger.info(
            "Request completed: request_id=%s status=%s",
            request_id,
            result.get("statusCode")
        )
        logger.info(
            "========== PRODUCT LAMBDA REQUEST END =========="
        )
        return result

    except ValueError as e:
        logger.error(
            "Validation error: request_id=%s method=%s path=%s message=%s",
            request_id,
            method,
            path,
            str(e)
        )

        result = response(
            400,
            {
                "message": str(e),
                "request_id": request_id
            }
        )

        logger.info(
            "========== PRODUCT LAMBDA REQUEST END: status=400 =========="
        )
        return result

    except pymysql.MySQLError as e:
        logger.exception(
            "Database error: request_id=%s method=%s path=%s error=%s",
            request_id,
            method,
            path,
            str(e)
        )

        result = response(
            500,
            {
                "message": "Database operation failed. Check CloudWatch logs using the request_id for details.",
                "request_id": request_id,
                "error": str(e)
            }
        )

        logger.info(
            "========== PRODUCT LAMBDA REQUEST END: status=500 DATABASE ERROR =========="
        )
        return result

    except Exception as e:
        logger.exception(
            "Unexpected Product API error: request_id=%s method=%s path=%s error=%s",
            request_id,
            method,
            path,
            str(e)
        )

        result = response(
            500,
            {
                "message": "Product operation failed. Check CloudWatch logs using the request_id for details.",
                "request_id": request_id,
                "error": str(e)
            }
        )

        logger.info(
            "========== PRODUCT LAMBDA REQUEST END: status=500 =========="
        )
        return result
