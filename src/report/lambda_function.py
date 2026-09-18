import os
import json
import logging
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
        logger.info("Reading SSM parameter: %s", parameter_name)

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
    """
    Create a connection to the CloudMart RDS MySQL database.
    """

    logger.info("Loading database configuration from SSM")

    host = get_parameter(DB_HOST_PARAMETER)
    port = int(get_parameter(DB_PORT_PARAMETER))
    database = get_parameter(DB_NAME_PARAMETER)
    username = get_parameter(DB_USERNAME_PARAMETER)
    password = get_parameter(DB_PASSWORD_PARAMETER)

    logger.info(
        "Connecting to CloudMart database at %s:%s",
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

    logger.info("Successfully connected to CloudMart RDS")

    return connection


# ==========================================================
# DATABASE TEST
# ==========================================================

def test_database(connection):
    """
    Verify that the database is accessible.
    """

    logger.info("Testing database connection")

    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 AS connection_test")
        result = cursor.fetchone()

    logger.info(
        "Database connection test result: %s",
        result
    )

    return result


# ==========================================================
# S3 TEST
# ==========================================================

def test_s3_access():
    """
    Verify that the Lambda can access the report bucket.
    """

    bucket_name = get_parameter(REPORT_BUCKET_PARAMETER)

    logger.info(
        "Testing access to report S3 bucket: %s",
        bucket_name
    )

    test_key = (
        f"reports/test/"
        f"report-lambda-test-{ENVIRONMENT}.txt"
    )

    s3.put_object(
        Bucket=bucket_name,
        Key=test_key,
        Body="CloudMart Report Lambda S3 connectivity test."
    )

    logger.info(
        "Successfully uploaded test object to s3://%s/%s",
        bucket_name,
        test_key
    )

    return {
        "bucket": bucket_name,
        "key": test_key
    }


# ==========================================================
# LAMBDA HANDLER
# ==========================================================

def lambda_handler(event, context):

    logger.info("Report Lambda execution started")

    logger.info(
        "Environment: %s",
        ENVIRONMENT
    )

    connection = None

    try:

        # --------------------------------------------------
        # TEST DATABASE
        # --------------------------------------------------

        connection = get_database_connection()

        database_result = test_database(connection)


        # --------------------------------------------------
        # TEST S3
        # --------------------------------------------------

        s3_result = test_s3_access()


        # --------------------------------------------------
        # SUCCESS
        # --------------------------------------------------

        response = {
            "status": "success",
            "message": "Report Lambda connectivity test successful",
            "database": database_result,
            "s3": s3_result
        }

        logger.info(
            "Report Lambda execution completed successfully"
        )

        return {
            "statusCode": 200,
            "body": json.dumps(response)
        }

    except Exception as exc:

        logger.exception(
            "Report Lambda execution failed: %s",
            exc
        )

        return {
            "statusCode": 500,
            "body": json.dumps({
                "status": "error",
                "message": "Report Lambda execution failed"
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