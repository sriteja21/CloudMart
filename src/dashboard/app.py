import os
import logging
from datetime import datetime, timezone
from decimal import Decimal

import boto3
import requests
from botocore.exceptions import BotoCoreError, ClientError
from flask import Flask, jsonify, render_template, request, send_file

app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("cloudmart-dashboard")

ENVIRONMENT = os.getenv("ENVIRONMENT", "dev")
API_URL = os.getenv("API_URL", "").rstrip("/")
REPORT_BUCKET = os.getenv("REPORT_BUCKET", "")
AUTH_PARAMETER = os.getenv(
    "AUTH_PARAMETER",
    f"/app/{ENVIRONMENT}/auth/token",
)

ssm = boto3.client("ssm")
s3 = boto3.client("s3")
cloudwatch = boto3.client("cloudwatch")


def get_auth_token():
    """Read the dashboard's API token from SSM; never expose it to the browser."""
    if not AUTH_PARAMETER:
        return None

    response = ssm.get_parameter(
        Name=AUTH_PARAMETER,
        WithDecryption=True,
    )
    return response["Parameter"]["Value"]


def api_get(path):
    """Call a CloudMart API endpoint from the EC2 backend."""
    if not API_URL:
        raise RuntimeError("API_URL is not configured")

    token = get_auth_token()
    if not token:
        raise RuntimeError("Dashboard API token is not configured")

    url = f"{API_URL}/{path.lstrip('/')}"
    logger.info("Dashboard API GET %s", path)

    response = requests.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    response.raise_for_status()
    return response.json()


def api_post(path, payload):
    token = get_auth_token()
    url = f"{API_URL}/{path.lstrip('/')}"
    response = requests.post(
        url,
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    response.raise_for_status()
    return response.json()


def safe_decimal(value):
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal("0")


def dashboard_data():
    products = api_get("/product").get("products", [])

    # These endpoints are expected from the CloudMart API.
    # If an endpoint is unavailable, the dashboard still loads.
    customers = []
    orders = []

    try:
        customers = api_get("/customer").get("customers", [])
    except Exception as exc:
        logger.warning("Customer API unavailable: %s", exc)

    try:
        orders = api_get("/order").get("orders", [])
    except Exception as exc:
        logger.warning("Order API unavailable: %s", exc)

    total_inventory = sum(
        int(p.get("quantity_available", 0) or 0)
        for p in products
    )

    low_stock = [
        p for p in products
        if int(p.get("quantity_available", 0) or 0)
        <= int(p.get("reorder_threshold", 0) or 0)
    ]

    total_revenue = Decimal("0")
    for order in orders:
        status = str(order.get("status", "")).upper()
        if status not in {"FAILED", "CANCELLED"}:
            total_revenue += safe_decimal(
                order.get("total_amount", order.get("total", 0))
            )

    product_values = sorted(
        products,
        key=lambda p: (
            safe_decimal(p.get("price", 0))
            * int(p.get("quantity_available", 0) or 0)
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
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


@app.route("/")
def index():
    try:
        data = dashboard_data()
        return render_template("index.html", data=data)
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
            },
        )


@app.route("/health")
def health():
    return jsonify({
        "status": "healthy",
        "environment": ENVIRONMENT,
    })


@app.route("/api/summary")
def summary():
    try:
        return jsonify(dashboard_data())
    except Exception as exc:
        logger.exception("Summary API failed")
        return jsonify({"error": str(exc)}), 500


@app.route("/product/<int:product_id>")
def product_detail(product_id):
    try:
        data = api_get(f"/product/{product_id}")
        return render_template("product_detail.html", product=data.get("product", data))
    except Exception as exc:
        logger.exception("Product detail failed: product_id=%s", product_id)
        return render_template(
            "product_detail.html",
            product={"product_id": product_id, "error": str(exc)},
        )


@app.route("/customer/<int:customer_id>")
def customer_detail(customer_id):
    try:
        data = api_get(f"/customer/{customer_id}")
        return render_template(
            "customer_detail.html",
            customer=data.get("customer", data),
        )
    except Exception as exc:
        logger.exception("Customer detail failed: customer_id=%s", customer_id)
        return render_template(
            "customer_detail.html",
            customer={"customer_id": customer_id, "error": str(exc)},
        )


@app.route("/order/<int:order_id>")
def order_detail(order_id):
    try:
        data = api_get(f"/order/{order_id}")
        return render_template(
            "order_detail.html",
            order=data.get("order", data),
        )
    except Exception as exc:
        logger.exception("Order detail failed: order_id=%s", order_id)
        return render_template(
            "order_detail.html",
            order={"order_id": order_id, "error": str(exc)},
        )


@app.route("/reports")
def reports():
    reports_list = []

    if REPORT_BUCKET:
        try:
            response = s3.list_objects_v2(Bucket=REPORT_BUCKET)
            for obj in response.get("Contents", []):
                reports_list.append({
                    "key": obj["Key"],
                    "size": obj["Size"],
                    "last_modified": obj["LastModified"].isoformat(),
                })
        except (BotoCoreError, ClientError) as exc:
            logger.exception("Unable to list reports")
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
def download_report():
    key = request.args.get("key", "").strip()

    if not key:
        return jsonify({"error": "Report key is required"}), 400

    # Prevent path-like values from being used as a local filename.
    filename = os.path.basename(key)

    try:
        obj = s3.get_object(
            Bucket=REPORT_BUCKET,
            Key=key,
        )

        from io import BytesIO
        return send_file(
            BytesIO(obj["Body"].read()),
            as_attachment=True,
            download_name=filename,
            mimetype="text/csv",
        )
    except (BotoCoreError, ClientError) as exc:
        logger.exception("Report download failed: key=%s", key)
        return jsonify({"error": "Unable to download report"}), 404


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8000)
