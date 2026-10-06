import os
import logging
import hashlib
from functools import wraps
from datetime import datetime, timezone
from decimal import Decimal
from io import BytesIO

import boto3
import pymysql
from botocore.exceptions import BotoCoreError, ClientError
from flask import (
    Flask,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)


app = Flask(__name__)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

logger = logging.getLogger("cloudmart-dashboard")


# ==========================================================
# APPLICATION CONFIGURATION
# ==========================================================

ENVIRONMENT = os.getenv("ENVIRONMENT", "dev")
REPORT_BUCKET = os.getenv("REPORT_BUCKET", "")

# Set this in the EC2 systemd environment.
# A random fallback keeps the application running, but sessions will
# become invalid whenever the process restarts.
DASHBOARD_SECRET_KEY = os.getenv("DASHBOARD_SECRET_KEY")
if not DASHBOARD_SECRET_KEY:
    logger.warning(
        "DASHBOARD_SECRET_KEY is not configured. "
        "A temporary secret will be generated for this process."
    )
    DASHBOARD_SECRET_KEY = os.urandom(32)

app.secret_key = DASHBOARD_SECRET_KEY
app.config["ENVIRONMENT"] = ENVIRONMENT

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("SESSION_COOKIE_SECURE", "false").lower()
    == "true",
    PERMANENT_SESSION_LIFETIME=3600,
)


# ==========================================================
# AWS CLIENTS
# ==========================================================

ssm = boto3.client("ssm")
s3 = boto3.client("s3")
cloudwatch = boto3.client("cloudwatch")


# ==========================================================
# DATABASE SSM PARAMETERS
# ==========================================================

DB_PARAMETER_NAMES = {
    "host": f"/app/{ENVIRONMENT}/database/host",
    "port": f"/app/{ENVIRONMENT}/database/port",
    "name": f"/app/{ENVIRONMENT}/database/name",
    "username": f"/app/{ENVIRONMENT}/database/username",
    "password": f"/app/{ENVIRONMENT}/database/password",
}


# ==========================================================
# ADMIN LOGIN
# ==========================================================


def login_required(view):
    """Require an authenticated ADMIN session before accessing the dashboard."""

    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if not session.get("authenticated"):
            return redirect(url_for("login", next=request.path))

        if session.get("role") != "ADMIN":
            session.clear()
            return redirect(url_for("login"))

        return view(*args, **kwargs)

    return wrapped_view


def hash_login_token(token):
    """Return the SHA-256 hash used by the customers.token_hash column."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@app.route("/login", methods=["GET", "POST"])
def login():
    """Authenticate an ADMIN user directly against the customers table."""

    if session.get("authenticated") and session.get("role") == "ADMIN":
        next_url = request.form.get("next", "").strip()
        if next_url.startswith("/") and not next_url.startswith("//"):
            return redirect(next_url)

        return redirect(url_for("index"))

    if request.method == "GET":
        return render_template("login.html", error=None, next=request.args.get("next", ""))

    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")

    if not email or not password:
        return render_template(
            "login.html",
            error="Email and admin token are required.",
            next=request.form.get("next", ""),
        ), 400

    connection = None

    try:
        connection = get_db_connection()

        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT customer_id, email, name, role, is_active, token_hash
                FROM customers
                WHERE email = %s
                LIMIT 1
                """,
                (email,),
            )
            customer = cursor.fetchone()

        if not customer:
            logger.warning("Admin login failed: unknown email=%s", email)
            return render_template(
                "login.html",
                error="Invalid admin credentials.",
                next=request.form.get("next", ""),
            ), 401

        expected_hash = customer["token_hash"]
        supplied_hash = hash_login_token(password)

        if not expected_hash or supplied_hash != expected_hash:
            logger.warning(
                "Admin login failed: invalid credentials customer_id=%s",
                customer["customer_id"],
            )
            return render_template(
                "login.html",
                error="Invalid admin credentials.",
                next=request.form.get("next", ""),
            ), 401

        if not customer["is_active"]:
            logger.warning(
                "Admin login failed: inactive customer_id=%s",
                customer["customer_id"],
            )
            return render_template(
                "login.html",
                error="This account is inactive.",
                next=request.form.get("next", ""),
            ), 403

        if str(customer["role"]).upper() != "ADMIN":
            logger.warning(
                "Dashboard login rejected non-admin customer_id=%s role=%s",
                customer["customer_id"],
                customer["role"],
            )
            return render_template(
                "login.html",
                error="Only ADMIN users can access this dashboard.",
                next=request.form.get("next", ""),
            ), 403

        session.clear()
        session.permanent = True
        session["authenticated"] = True
        session["customer_id"] = int(customer["customer_id"])
        session["email"] = customer["email"]
        session["name"] = customer["name"]
        session["role"] = "ADMIN"

        logger.info(
            "Admin login successful: customer_id=%s email=%s",
            customer["customer_id"],
            customer["email"],
        )

        next_url = request.args.get("next", "")
        if next_url.startswith("/") and not next_url.startswith("//"):
            return redirect(next_url)

        return redirect(url_for("index"))

    except Exception:
        logger.exception("Admin login failed due to database/application error")
        return render_template_string(
            LOGIN_HTML,
            error="Unable to authenticate right now. Please try again.",
        ), 500

    finally:
        if connection:
            connection.close()


@app.route("/logout", methods=["GET", "POST"])
def logout():
    """Clear the admin session."""

    email = session.get("email")
    session.clear()

    logger.info("Admin logout completed: email=%s", email)

    return redirect(url_for("login"))


# ==========================================================
# DATABASE CONNECTION
# ==========================================================


def get_db_connection():
    """Read RDS credentials from SSM and create a MySQL connection."""

    response = ssm.get_parameters(
        Names=list(DB_PARAMETER_NAMES.values()),
        WithDecryption=True,
    )

    values = {
        parameter["Name"]: parameter["Value"]
        for parameter in response.get("Parameters", [])
    }

    missing = [
        name
        for name in DB_PARAMETER_NAMES.values()
        if name not in values
    ]

    if missing:
        raise RuntimeError(
            "Missing database SSM parameters: "
            + ", ".join(missing)
        )

    host = values[DB_PARAMETER_NAMES["host"]]
    port = int(values[DB_PARAMETER_NAMES["port"]])
    database = values[DB_PARAMETER_NAMES["name"]]
    username = values[DB_PARAMETER_NAMES["username"]]
    password = values[DB_PARAMETER_NAMES["password"]]

    logger.info(
        "Connecting dashboard to RDS database host=%s database=%s",
        host,
        database,
    )

    return pymysql.connect(
        host=host,
        port=port,
        user=username,
        password=password,
        database=database,
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=10,
        read_timeout=20,
        write_timeout=20,
        autocommit=True,
    )


# ==========================================================
# DATABASE HELPERS
# ==========================================================


def fetch_all(connection, query, params=None):
    """Execute a SELECT and return all rows as dictionaries."""

    with connection.cursor() as cursor:
        cursor.execute(query, params or ())
        return cursor.fetchall()


def fetch_one(connection, query, params=None):
    """Execute a SELECT and return one row as a dictionary."""

    with connection.cursor() as cursor:
        cursor.execute(query, params or ())
        return cursor.fetchone()


def safe_decimal(value):
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal("0")


# ==========================================================
# DASHBOARD DATA - DIRECT RDS ACCESS
# ==========================================================


def dashboard_data():
    """
    Load dashboard data directly from the CloudMart RDS MySQL database.

    No API Gateway, Lambda authorizer, or CloudMart API is used here.
    The Flask application connects directly to RDS using database
    credentials retrieved from SSM Parameter Store.
    """

    connection = None
    warnings = []

    try:
        connection = get_db_connection()

        # --------------------------------------------------
        # PRODUCTS + INVENTORY
        # --------------------------------------------------
        products = fetch_all(
            connection,
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
                COALESCE(i.quantity_available, 0) AS quantity_available,
                COALESCE(i.reorder_threshold, 0) AS reorder_threshold,
                i.last_updated_at
            FROM products p
            LEFT JOIN inventory i
                ON p.product_id = i.product_id
            WHERE p.is_active = TRUE
            ORDER BY p.product_id ASC
            """,
        )

        logger.info(
            "Dashboard loaded products directly from RDS: count=%s",
            len(products),
        )

        # --------------------------------------------------
        # CUSTOMERS
        # --------------------------------------------------
        try:
            customers = fetch_all(
                connection,
                """
                SELECT
                    customer_id,
                    email,
                    name,
                    role,
                    is_active,
                    created_at,
                    updated_at
                FROM customers
                ORDER BY customer_id ASC
                """,
            )

            logger.info(
                "Dashboard loaded customers directly from RDS: count=%s",
                len(customers),
            )

        except Exception as exc:
            logger.exception("Customer database query failed")
            customers = []
            warnings.append(
                f"Customers could not be loaded: {exc}"
            )

        # --------------------------------------------------
        # ORDERS + CUSTOMER INFORMATION
        # --------------------------------------------------
        try:
            orders = fetch_all(
                connection,
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
                LEFT JOIN customers c
                    ON o.customer_id = c.customer_id
                ORDER BY o.created_at DESC, o.order_id DESC
                """,
            )

            logger.info(
                "Dashboard loaded orders directly from RDS: count=%s",
                len(orders),
            )

        except Exception as exc:
            logger.exception("Order database query failed")
            orders = []
            warnings.append(
                f"Orders could not be loaded: {exc}"
            )

        # --------------------------------------------------
        # TOTAL INVENTORY
        # --------------------------------------------------
        total_inventory = sum(
            int(p.get("quantity_available", 0) or 0)
            for p in products
        )

        # --------------------------------------------------
        # LOW STOCK PRODUCTS
        # --------------------------------------------------
        low_stock = [
            p
            for p in products
            if int(p.get("quantity_available", 0) or 0)
            <= int(p.get("reorder_threshold", 0) or 0)
        ]

        # --------------------------------------------------
        # TOTAL REVENUE
        # --------------------------------------------------
        total_revenue = Decimal("0")

        for order in orders:
            status = str(
                order.get("status", "")
            ).upper()

            if status not in {"FAILED", "CANCELLED"}:
                total_revenue += safe_decimal(
                    order.get("total_amount", 0)
                )

        # --------------------------------------------------
        # TOP PRODUCTS BY INVENTORY VALUE
        # --------------------------------------------------
        product_values = sorted(
            products,
            key=lambda p: (
                safe_decimal(p.get("price", 0))
                * int(
                    p.get("quantity_available", 0)
                    or 0
                )
            ),
            reverse=True,
        )

        return {
            "products": products,
            "customers": customers,
            "orders": orders,
            "low_stock": low_stock,
            "total_inventory": total_inventory,
            "total_revenue": float(total_revenue),
            "top_products": product_values[:5],
            "generated_at": datetime.now(
                timezone.utc
            ).isoformat(),
            "warnings": warnings,
        }

    finally:
        if connection:
            connection.close()
            logger.info("Dashboard RDS connection closed")


# ==========================================================
# DASHBOARD ROUTES
# ==========================================================


@app.route("/")
@login_required
def index():
    try:
        data = dashboard_data()
        return render_template(
            "index.html",
            data=data,
            admin_name=session.get("name"),
            admin_email=session.get("email"),
        )

    except Exception as exc:
        logger.exception("Dashboard load failed")

        return render_template(
            "index.html",
            data={
                "products": [],
                "customers": [],
                "orders": [],
                "low_stock": [],
                "total_inventory": 0,
                "total_revenue": 0,
                "top_products": [],
                "generated_at": None,
                "error": str(exc),
                "warnings": [],
            },
            admin_name=session.get("name"),
            admin_email=session.get("email"),
        )


@app.route("/health")
def health():
    return jsonify({
        "status": "healthy",
        "environment": ENVIRONMENT,
    })


@app.route("/api/summary")
@login_required
def summary():
    try:
        return jsonify(dashboard_data())

    except Exception as exc:
        logger.exception("Summary API failed")
        return jsonify({"error": str(exc)}), 500


# ==========================================================
# PRODUCT DETAIL - DIRECT RDS ACCESS
# ==========================================================


@app.route("/product/<int:product_id>")
@login_required
def product_detail(product_id):
    connection = None

    try:
        connection = get_db_connection()

        product = fetch_one(
            connection,
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
                COALESCE(i.quantity_available, 0) AS quantity_available,
                COALESCE(i.reorder_threshold, 0) AS reorder_threshold,
                i.last_updated_at
            FROM products p
            LEFT JOIN inventory i
                ON p.product_id = i.product_id
            WHERE p.product_id = %s
            LIMIT 1
            """,
            (product_id,),
        )

        if not product:
            product = {
                "product_id": product_id,
                "error": "Product not found",
            }

        return render_template(
            "product_detail.html",
            product=product,
        )

    except Exception as exc:
        logger.exception(
            "Product detail failed: product_id=%s",
            product_id,
        )

        return render_template(
            "product_detail.html",
            product={
                "product_id": product_id,
                "error": str(exc),
            },
        )

    finally:
        if connection:
            connection.close()


# ==========================================================
# CUSTOMER DETAIL - DIRECT RDS ACCESS
# ==========================================================


@app.route("/customer/<int:customer_id>")
@login_required
def customer_detail(customer_id):
    connection = None

    try:
        connection = get_db_connection()

        customer = fetch_one(
            connection,
            """
            SELECT
                customer_id,
                email,
                name,
                role,
                is_active,
                created_at,
                updated_at
            FROM customers
            WHERE customer_id = %s
            LIMIT 1
            """,
            (customer_id,),
        )

        if not customer:
            customer = {
                "customer_id": customer_id,
                "error": "Customer not found",
            }

        return render_template(
            "customer_detail.html",
            customer=customer,
        )

    except Exception as exc:
        logger.exception(
            "Customer detail failed: customer_id=%s",
            customer_id,
        )

        return render_template(
            "customer_detail.html",
            customer={
                "customer_id": customer_id,
                "error": str(exc),
            },
        )

    finally:
        if connection:
            connection.close()


# ==========================================================
# ORDER DETAIL - DIRECT RDS ACCESS
# ==========================================================


@app.route("/order/<int:order_id>")
@login_required
def order_detail(order_id):
    connection = None

    try:
        connection = get_db_connection()

        order = fetch_one(
            connection,
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
            LEFT JOIN customers c
                ON o.customer_id = c.customer_id
            WHERE o.order_id = %s
            LIMIT 1
            """,
            (order_id,),
        )

        if not order:
            order = {
                "order_id": order_id,
                "error": "Order not found",
            }
        else:
            order["items"] = fetch_all(
                connection,
                """
                SELECT
                    oi.order_item_id,
                    oi.order_id,
                    oi.product_id,
                    p.name AS product_name,
                    oi.quantity,
                    oi.unit_price,
                    oi.total_price
                FROM order_items oi
                LEFT JOIN products p
                    ON oi.product_id = p.product_id
                WHERE oi.order_id = %s
                ORDER BY oi.order_item_id ASC
                """,
                (order_id,),
            )

            order["logs"] = fetch_all(
                connection,
                """
                SELECT
                    log_id,
                    order_id,
                    event_type,
                    old_status,
                    new_status,
                    message,
                    created_at
                FROM order_logs
                WHERE order_id = %s
                ORDER BY created_at DESC, log_id DESC
                """,
                (order_id,),
            )

        return render_template(
            "order_detail.html",
            order=order,
        )

    except Exception as exc:
        logger.exception(
            "Order detail failed: order_id=%s",
            order_id,
        )

        return render_template(
            "order_detail.html",
            order={
                "order_id": order_id,
                "error": str(exc),
            },
        )

    finally:
        if connection:
            connection.close()


# ==========================================================
# REPORTS
# ==========================================================


@app.route("/reports")
@login_required
def reports():
    reports_list = []

    if REPORT_BUCKET:
        try:
            response = s3.list_objects_v2(
                Bucket=REPORT_BUCKET,
            )

            for obj in response.get("Contents", []):
                reports_list.append({
                    "key": obj["Key"],
                    "size": obj["Size"],
                    "last_modified": obj[
                        "LastModified"
                    ].isoformat(),
                })

        except (
            BotoCoreError,
            ClientError,
        ) as exc:
            logger.exception(
                "Unable to list reports"
            )

            return render_template(
                "reports.html",
                reports=[],
                error=str(exc),
            )

    reports_list.sort(
        key=lambda item: item["last_modified"],
        reverse=True,
    )

    return render_template(
        "reports.html",
        reports=reports_list,
        error=None,
    )


@app.route("/reports/download")
@login_required
def download_report():
    key = request.args.get(
        "key",
        "",
    ).strip()

    if not key:
        return jsonify({
            "error": "Report key is required"
        }), 400

    # Prevent path-like values from being used as a local filename.
    filename = os.path.basename(key)

    try:
        obj = s3.get_object(
            Bucket=REPORT_BUCKET,
            Key=key,
        )

        return send_file(
            BytesIO(
                obj["Body"].read()
            ),
            as_attachment=True,
            download_name=filename,
            mimetype="text/csv",
        )

    except (
        BotoCoreError,
        ClientError,
    ) as exc:
        logger.exception(
            "Report download failed: key=%s",
            key,
        )

        return jsonify({
            "error": "Unable to download report"
        }), 404


# ==========================================================
# APPLICATION ENTRY POINT
# ==========================================================


if __name__ == "__main__":
    app.run(
        host="127.0.0.1",
        port=8000,
    )
