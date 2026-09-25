import logging
import os

import boto3
import pymysql
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ssm = boto3.client("ssm")

ENVIRONMENT = os.getenv("ENVIRONMENT", "dev")

SCHEMA_FILE = os.path.join(
    os.path.dirname(__file__),
    "schema.sql"
)

PARAMETERS = {
    "host": f"/app/{ENVIRONMENT}/database/host",
    "port": f"/app/{ENVIRONMENT}/database/port",
    "database": f"/app/{ENVIRONMENT}/database/name",
    "username": f"/app/{ENVIRONMENT}/database/username",
    "password": f"/app/{ENVIRONMENT}/database/password",
}


# ==========================================================
# SSM PARAMETER
# ==========================================================

def get_parameter(name):
    parameter_name = PARAMETERS[name]

    logger.info(
        "Reading database parameter: %s",
        parameter_name
    )

    try:
        response = ssm.get_parameter(
            Name=parameter_name,
            WithDecryption=True
        )

        value = response["Parameter"]["Value"]

        logger.info(
            "Database parameter loaded successfully: %s",
            name
        )

        return value

    except ClientError:
        logger.exception(
            "Failed to read database parameter: %s",
            name
        )
        raise


# ==========================================================
# DATABASE CONFIGURATION
# ==========================================================

def get_database_config():

    logger.info(
        "Loading database configuration for environment: %s",
        ENVIRONMENT
    )

    config = {
        "host": get_parameter("host"),
        "port": int(get_parameter("port")),
        "database": get_parameter("database"),
        "username": get_parameter("username"),
        "password": get_parameter("password")
    }

    logger.info(
        "Database configuration loaded successfully: "
        "host=%s port=%s database=%s username=%s",
        config["host"],
        config["port"],
        config["database"],
        config["username"]
    )

    return config


# ==========================================================
# CONNECT TO MYSQL SERVER
# ==========================================================

def connect_to_server(config):

    logger.info("Connecting to MySQL server")

    connection = pymysql.connect(
        host=config["host"],
        port=config["port"],
        user=config["username"],
        password=config["password"],
        charset="utf8mb4",
        autocommit=True,
        connect_timeout=10
    )

    logger.info(
        "Successfully connected to MySQL server"
    )

    return connection


# ==========================================================
# CONNECT TO DATABASE
# ==========================================================

def connect_to_database(config):

    logger.info(
        "Connecting to database: %s",
        config["database"]
    )

    connection = pymysql.connect(
        host=config["host"],
        port=config["port"],
        user=config["username"],
        password=config["password"],
        database=config["database"],
        charset="utf8mb4",
        autocommit=False,
        connect_timeout=10
    )

    logger.info(
        "Successfully connected to database: %s",
        config["database"]
    )

    return connection


# ==========================================================
# CREATE DATABASE
# ==========================================================

def create_database(config):

    logger.info(
        "Checking whether database exists: %s",
        config["database"]
    )

    connection = connect_to_server(config)

    try:

        database_name = config["database"].replace("`", "``")

        with connection.cursor() as cursor:

            cursor.execute(
                f"""
                CREATE DATABASE IF NOT EXISTS `{database_name}`
                """
            )

        logger.info(
            "Database is ready: %s",
            config["database"]
        )

    finally:

        connection.close()

        logger.info(
            "MySQL server connection closed"
        )


# ==========================================================
# EXECUTE SCHEMA.SQL
# ==========================================================

def execute_schema(config):

    logger.info(
        "Starting schema execution"
    )

    if not os.path.exists(SCHEMA_FILE):

        logger.error(
            "schema.sql was not found: %s",
            SCHEMA_FILE
        )

        raise FileNotFoundError(
            f"schema.sql not found: {SCHEMA_FILE}"
        )

    logger.info(
        "Reading schema file: %s",
        SCHEMA_FILE
    )

    with open(
        SCHEMA_FILE,
        "r",
        encoding="utf-8"
    ) as file:

        sql = file.read()

    connection = connect_to_database(config)

    executed = 0

    try:

        with connection.cursor() as cursor:

            statements = sql.split(";")

            logger.info(
                "Found %s SQL statements in schema.sql",
                len(statements)
            )

            for statement in statements:

                statement = statement.strip()

                if not statement:
                    continue

                # Ignore comment-only statements
                if statement.startswith("--"):
                    continue

                logger.info(
                    "Executing SQL statement %s",
                    executed + 1
                )

                cursor.execute(statement)

                executed += 1

        connection.commit()

        logger.info(
            "Schema execution completed successfully. "
            "Statements executed: %s",
            executed
        )

    except Exception:

        connection.rollback()

        logger.exception(
            "Schema execution failed. "
            "Transaction rolled back."
        )

        raise

    finally:

        connection.close()

        logger.info(
            "Database connection closed"
        )

    return executed


# ==========================================================
# LAMBDA HANDLER
# ==========================================================

def lambda_handler(event, context):

    request_id = getattr(
        context,
        "aws_request_id",
        "unknown"
    )

    logger.info(
        "=================================================="
    )

    logger.info(
        "Database initialization Lambda started"
    )

    logger.info(
        "Request ID: %s",
        request_id
    )

    logger.info(
        "Environment: %s",
        ENVIRONMENT
    )

    try:

        # --------------------------------------------------
        # 1. Load database configuration
        # --------------------------------------------------

        config = get_database_config()

        # --------------------------------------------------
        # 2. Create database if required
        # --------------------------------------------------

        create_database(config)

        # --------------------------------------------------
        # 3. Execute schema.sql
        # --------------------------------------------------

        statements_executed = execute_schema(
            config
        )

        logger.info(
            "Database initialization completed successfully"
        )

        logger.info(
            "Statements executed: %s",
            statements_executed
        )

        logger.info(
            "=================================================="
        )

        return {
            "statusCode": 200,
            "message": "Database initialization completed successfully.",
            "environment": ENVIRONMENT,
            "database": config["database"],
            "statements_executed": statements_executed,
            "request_id": request_id
        }

    except Exception as exc:

        logger.exception(
            "Database initialization failed: %s",
            str(exc)
        )

        logger.info(
            "=================================================="
        )

        raise