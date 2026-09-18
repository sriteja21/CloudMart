import os
import csv
import io
import json
import logging
from datetime import datetime, timezone

import boto3
import pymysql


# ==========================================================
# LOGGING
# ==========================================================

logger = logging.getLogger()
logger.setLevel(logging.INFO)


# ==========================================================
# AWS CLIENTS
# ==========================================================

ssm = boto3.client("ssm")
s3 = boto3.client("s3")


# ==========================================================
# ENVIRONMENT
# ==========================================================

ENVIRONMENT = os.environ.get("ENVIRONMENT", "dev")


# ==========================================================
# SSM PARAMETERS
# ==========================================================

DB_HOST_PARAMETER = f"/app/{ENVIRONMENT}/database/host"
DB_PORT_PARAMETER = f"/app/{ENVIRONMENT}/database/port"
DB_NAME_PARAMETER = f"/app/{ENVIRONMENT}/database/name"
DB_USERNAME_PARAMETER = f"/app/{ENVIRONMENT}/database/username"
DB_PASSWORD_PARAMETER = f"/app/{ENVIRONMENT}/database/password"

REPORT_BUCKET_PARAMETER = f"/app/{ENVIRONMENT}/s3/report-bucket"


# ==========================================================
# SSM HELPER
# ==========================================================

def get_parameter(parameter_name):
    """
    Retrieve a parameter from AWS Systems Manager Parameter Store.
    """

    try:

        logger.info(
            "Reading SSM parameter: %s",
            parameter_name
        )

        response = ssm.get_parameter(
            Name=parameter_name,
            WithDecryption=True
        )

        logger.info(
            "Successfully retrieved SSM parameter: %s",
            parameter_name
        )

        return response["Parameter"]["Value"]

    except Exception:

        logger.exception(
            "Failed to retrieve SSM parameter: %s",
            parameter_name
        )

        raise


# ==========================================================
# DATABASE CONNECTION
# ==========================================================

def get_database_connection():

    try:

        logger.info(
            "Loading database configuration from SSM"
        )

        host = get_parameter(DB_HOST_PARAMETER)
        port = int(get_parameter(DB_PORT_PARAMETER))
        database = get_parameter(DB_NAME_PARAMETER)
        username = get_parameter(DB_USERNAME_PARAMETER)
        password = get_parameter(DB_PASSWORD_PARAMETER)

        logger.info(
            "Connecting to CloudMart RDS MySQL at %s:%s",
            host,
            port
        )

        connection = pymysql.connect(
            host=host,
            port=port,
            user=username,
            password=password,
            database=database,
            connect_timeout=10,
            read_timeout=30,
            write_timeout=30,
            cursorclass=pymysql.cursors.DictCursor
        )

        logger.info(
            "Successfully connected to CloudMart RDS"
        )

        return connection

    except Exception:

        logger.exception(
            "Failed to establish database connection"
        )

        raise


# ==========================================================
# FETCH INVENTORY DATA
# ==========================================================

def get_inventory_report(connection):

    logger.info(
        "Fetching inventory report data"
    )

    query = """
        SELECT
            p.product_id,
            p.name AS product_name,
            p.category,
            p.price,
            p.is_active,
            i.quantity_available,
            i.reorder_threshold,
            i.last_updated_at,
            CASE
                WHEN i.quantity_available <= i.reorder_threshold
                THEN 'LOW_STOCK'
                ELSE 'OK'
            END AS stock_status
        FROM products p
        LEFT JOIN inventory i
            ON p.product_id = i.product_id
        ORDER BY p.product_id
    """

    try:

        with connection.cursor() as cursor:

            cursor.execute(query)

            rows = cursor.fetchall()

        logger.info(
            "Fetched %d inventory records",
            len(rows)
        )

        return rows

    except Exception:

        logger.exception(
            "Failed to fetch inventory report data"
        )

        raise


# ==========================================================
# FETCH ORDERS
# ==========================================================

def get_orders_report(connection):

    logger.info(
        "Fetching orders report data"
    )

    query = """
        SELECT
            o.order_id,
            o.order_number,
            o.customer_id,
            c.name AS customer_name,
            c.email AS customer_email,
            o.status,
            o.total_amount,
            o.created_at,
            o.updated_at
        FROM orders o
        LEFT JOIN customers c
            ON o.customer_id = c.customer_id
        ORDER BY o.created_at DESC
    """

    try:

        with connection.cursor() as cursor:

            cursor.execute(query)

            rows = cursor.fetchall()

        logger.info(
            "Fetched %d order records",
            len(rows)
        )

        return rows

    except Exception:

        logger.exception(
            "Failed to fetch orders report data"
        )

        raise


# ==========================================================
# FETCH ORDER ITEMS
# ==========================================================

def get_order_items_report(connection):

    logger.info(
        "Fetching order item report data"
    )

    query = """
        SELECT
            oi.order_item_id,
            oi.order_id,
            o.order_number,
            oi.product_id,
            p.name AS product_name,
            p.category,
            oi.quantity,
            oi.unit_price,
            oi.total_price
        FROM order_items oi
        LEFT JOIN orders o
            ON oi.order_id = o.order_id
        LEFT JOIN products p
            ON oi.product_id = p.product_id
        ORDER BY oi.order_id DESC
    """

    try:

        with connection.cursor() as cursor:

            cursor.execute(query)

            rows = cursor.fetchall()

        logger.info(
            "Fetched %d order item records",
            len(rows)
        )

        return rows

    except Exception:

        logger.exception(
            "Failed to fetch order item report data"
        )

        raise


# ==========================================================
# FETCH ORDER LOGS
# ==========================================================

def get_order_logs_report(connection):

    logger.info(
        "Fetching order log report data"
    )

    query = """
        SELECT
            ol.log_id,
            ol.order_id,
            o.order_number,
            ol.event_type,
            ol.old_status,
            ol.new_status,
            ol.message,
            ol.created_at
        FROM order_logs ol
        LEFT JOIN orders o
            ON ol.order_id = o.order_id
        ORDER BY ol.created_at DESC
    """

    try:

        with connection.cursor() as cursor:

            cursor.execute(query)

            rows = cursor.fetchall()

        logger.info(
            "Fetched %d order log records",
            len(rows)
        )

        return rows

    except Exception:

        logger.exception(
            "Failed to fetch order log report data"
        )

        raise


# ==========================================================
# GENERATE SUMMARY
# ==========================================================

def generate_summary(
    inventory,
    orders,
    order_items,
    order_logs
):

    logger.info(
        "Generating report summary"
    )

    total_products = len(inventory)

    low_stock_products = sum(
        1
        for item in inventory
        if item.get("stock_status") == "LOW_STOCK"
    )

    active_products = sum(
        1
        for item in inventory
        if item.get("is_active")
    )

    total_orders = len(orders)

    total_revenue = sum(
        float(order.get("total_amount") or 0)
        for order in orders
    )

    total_items_sold = sum(
        int(item.get("quantity") or 0)
        for item in order_items
    )

    total_order_events = len(order_logs)

    summary = {

        "report_generated_at":
            datetime.now(timezone.utc).isoformat(),

        "environment":
            ENVIRONMENT,

        "total_products":
            total_products,

        "active_products":
            active_products,

        "low_stock_products":
            low_stock_products,

        "total_orders":
            total_orders,

        "total_revenue":
            round(total_revenue, 2),

        "total_items_sold":
            total_items_sold,

        "total_order_events":
            total_order_events
    }

    logger.info(
        "Report summary generated: %s",
        json.dumps(summary, default=str)
    )

    return summary


# ==========================================================
# CSV HELPERS
# ==========================================================

def write_csv(writer, title, rows, fields):

    writer.writerow([title])

    writer.writerow(fields)

    for row in rows:

        writer.writerow([
            format_csv_value(row.get(field))
            for field in fields
        ])

    writer.writerow([])


def format_csv_value(value):

    if value is None:
        return ""

    if isinstance(value, datetime):
        return value.isoformat()

    return str(value)


# ==========================================================
# GENERATE CSV
# ==========================================================

def generate_csv(
    summary,
    inventory,
    orders,
    order_items,
    order_logs
):

    logger.info(
        "Generating CSV report"
    )

    output = io.StringIO()

    writer = csv.writer(output)

    # ------------------------------------------------------
    # REPORT TITLE
    # ------------------------------------------------------

    writer.writerow([
        "CloudMart Daily Operational Report"
    ])

    writer.writerow([
        f"Environment: {ENVIRONMENT}"
    ])

    writer.writerow([
        f"Generated At: {summary['report_generated_at']}"
    ])

    writer.writerow([])


    # ------------------------------------------------------
    # SUMMARY
    # ------------------------------------------------------

    writer.writerow([
        "SUMMARY"
    ])

    writer.writerow([
        "Metric",
        "Value"
    ])

    for key, value in summary.items():

        writer.writerow([
            key,
            value
        ])

    writer.writerow([])


    # ------------------------------------------------------
    # INVENTORY
    # ------------------------------------------------------

    inventory_fields = [

        "product_id",
        "product_name",
        "category",
        "price",
        "is_active",
        "quantity_available",
        "reorder_threshold",
        "last_updated_at",
        "stock_status"
    ]

    write_csv(
        writer,
        "INVENTORY",
        inventory,
        inventory_fields
    )


    # ------------------------------------------------------
    # ORDERS
    # ------------------------------------------------------

    order_fields = [

        "order_id",
        "order_number",
        "customer_id",
        "customer_name",
        "customer_email",
        "status",
        "total_amount",
        "created_at",
        "updated_at"
    ]

    write_csv(
        writer,
        "ORDERS",
        orders,
        order_fields
    )


    # ------------------------------------------------------
    # ORDER ITEMS
    # ------------------------------------------------------

    order_item_fields = [

        "order_item_id",
        "order_id",
        "order_number",
        "product_id",
        "product_name",
        "category",
        "quantity",
        "unit_price",
        "total_price"
    ]

    write_csv(
        writer,
        "ORDER ITEMS",
        order_items,
        order_item_fields
    )


    # ------------------------------------------------------
    # ORDER LOGS
    # ------------------------------------------------------

    order_log_fields = [

        "log_id",
        "order_id",
        "order_number",
        "event_type",
        "old_status",
        "new_status",
        "message",
        "created_at"
    ]

    write_csv(
        writer,
        "ORDER LOGS",
        order_logs,
        order_log_fields
    )


    csv_data = output.getvalue()

    output.close()

    logger.info(
        "CSV report generated successfully. Size: %d bytes",
        len(csv_data.encode("utf-8"))
    )

    return csv_data


# ==========================================================
# UPLOAD REPORT TO S3
# ==========================================================

def upload_report_to_s3(csv_data):

    logger.info(
        "Loading report bucket name from SSM"
    )

    bucket_name = get_parameter(
        REPORT_BUCKET_PARAMETER
    )

    current_date = datetime.now(
        timezone.utc
    ).strftime("%Y-%m-%d")

    timestamp = datetime.now(
        timezone.utc
    ).strftime("%Y%m%d-%H%M%S")

    object_key = (
        f"reports/daily/"
        f"{current_date}/"
        f"cloudmart-daily-report-{timestamp}.csv"
    )

    logger.info(
        "Uploading report to s3://%s/%s",
        bucket_name,
        object_key
    )

    try:

        s3.put_object(
            Bucket=bucket_name,
            Key=object_key,
            Body=csv_data.encode("utf-8"),
            ContentType="text/csv"
        )

        logger.info(
            "Report uploaded successfully to S3"
        )

        return {
            "bucket": bucket_name,
            "key": object_key
        }

    except Exception:

        logger.exception(
            "Failed to upload report to S3"
        )

        raise


# ==========================================================
# LAMBDA HANDLER
# ==========================================================

def lambda_handler(event, context):

    logger.info(
        "=================================================="
    )

    logger.info(
        "CloudMart Report Lambda execution started"
    )

    logger.info(
        "Environment: %s",
        ENVIRONMENT
    )

    logger.info(
        "Event: %s",
        json.dumps(event, default=str)
    )

    connection = None

    try:

        # --------------------------------------------------
        # CONNECT TO DATABASE
        # --------------------------------------------------

        connection = get_database_connection()


        # --------------------------------------------------
        # FETCH REPORT DATA
        # --------------------------------------------------

        inventory = get_inventory_report(
            connection
        )

        orders = get_orders_report(
            connection
        )

        order_items = get_order_items_report(
            connection
        )

        order_logs = get_order_logs_report(
            connection
        )


        # --------------------------------------------------
        # GENERATE SUMMARY
        # --------------------------------------------------

        summary = generate_summary(
            inventory,
            orders,
            order_items,
            order_logs
        )


        # --------------------------------------------------
        # LOG FORMATTED DATA
        # --------------------------------------------------

        logger.info(
            "Formatted report summary:\n%s",
            json.dumps(
                summary,
                indent=2,
                default=str
            )
        )


        # --------------------------------------------------
        # GENERATE CSV
        # --------------------------------------------------

        csv_data = generate_csv(
            summary,
            inventory,
            orders,
            order_items,
            order_logs
        )


        # --------------------------------------------------
        # UPLOAD CSV TO S3
        # --------------------------------------------------

        s3_result = upload_report_to_s3(
            csv_data
        )


        # --------------------------------------------------
        # SUCCESS RESPONSE
        # --------------------------------------------------

        response = {

            "status": "success",

            "message":
                "CloudMart daily report generated successfully",

            "environment":
                ENVIRONMENT,

            "summary":
                summary,

            "s3":
                s3_result
        }

        logger.info(
            "CloudMart Report Lambda execution completed successfully"
        )

        logger.info(
            "=================================================="
        )

        return {

            "statusCode": 200,

            "body": json.dumps(
                response,
                default=str
            )
        }


    except Exception as exc:

        logger.exception(
            "CloudMart Report Lambda execution failed: %s",
            exc
        )

        logger.info(
            "=================================================="
        )

        return {

            "statusCode": 500,

            "body": json.dumps({

                "status": "error",

                "message":
                    "CloudMart daily report generation failed"

            })
        }


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
