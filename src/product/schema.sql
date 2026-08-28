-- ==========================================================
-- CloudMart Database Schema
-- MySQL / Amazon RDS
--
-- Based on the supplied CloudMart ER diagram.
--
-- Tables:
--   customers
--   products
--   inventory
--   orders
--   order_items
--   order_logs
--
-- The statements are idempotent so they can safely be executed
-- when the Lambda initializes the database for the first time.
-- ==========================================================

CREATE DATABASE IF NOT EXISTS cloudmart
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

USE cloudmart;


-- ==========================================================
-- CUSTOMERS
-- ==========================================================

CREATE TABLE IF NOT EXISTS customers (
    customer_id INT NOT NULL AUTO_INCREMENT,
    email VARCHAR(255) NOT NULL,
    name VARCHAR(255) NOT NULL,
    token_hash VARCHAR(255) NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,

    PRIMARY KEY (customer_id),
    UNIQUE KEY uq_customers_email (email)
) ENGINE=InnoDB;


-- ==========================================================
-- PRODUCTS
-- ==========================================================

CREATE TABLE IF NOT EXISTS products (
    product_id INT NOT NULL AUTO_INCREMENT,
    name VARCHAR(255) NOT NULL,
    description TEXT NULL,
    price DECIMAL(12,2) NOT NULL,
    category VARCHAR(100) NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,

    PRIMARY KEY (product_id),

    CONSTRAINT chk_products_price
        CHECK (price >= 0)
) ENGINE=InnoDB;


-- ==========================================================
-- INVENTORY
-- ==========================================================

CREATE TABLE IF NOT EXISTS inventory (
    inventory_id BIGINT NOT NULL AUTO_INCREMENT,
    product_id INT NOT NULL,
    quantity_available INT NOT NULL DEFAULT 0,
    reorder_threshold INT NOT NULL DEFAULT 10,
    last_updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,

    PRIMARY KEY (inventory_id),

    UNIQUE KEY uq_inventory_product (product_id),

    CONSTRAINT fk_inventory_product
        FOREIGN KEY (product_id)
        REFERENCES products(product_id)
        ON DELETE CASCADE
        ON UPDATE CASCADE,

    CONSTRAINT chk_inventory_quantity
        CHECK (quantity_available >= 0),

    CONSTRAINT chk_inventory_reorder_threshold
        CHECK (reorder_threshold >= 0)
) ENGINE=InnoDB;


-- ==========================================================
-- ORDERS
-- ==========================================================

CREATE TABLE IF NOT EXISTS orders (
    order_id INT NOT NULL AUTO_INCREMENT,
    customer_id INT NOT NULL,
    order_number VARCHAR(50) NOT NULL,
    status VARCHAR(50) NOT NULL,
    total_amount DECIMAL(12,2) NOT NULL DEFAULT 0.00,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,

    PRIMARY KEY (order_id),

    UNIQUE KEY uq_orders_order_number (order_number),

    KEY idx_orders_customer_id (customer_id),

    CONSTRAINT fk_orders_customer
        FOREIGN KEY (customer_id)
        REFERENCES customers(customer_id)
        ON DELETE RESTRICT
        ON UPDATE CASCADE,

    CONSTRAINT chk_orders_total_amount
        CHECK (total_amount >= 0)
) ENGINE=InnoDB;


-- ==========================================================
-- ORDER ITEMS
-- ==========================================================

CREATE TABLE IF NOT EXISTS order_items (
    order_item_id BIGINT NOT NULL AUTO_INCREMENT,
    order_id INT NOT NULL,
    product_id INT NOT NULL,
    quantity INT NOT NULL,
    unit_price DECIMAL(12,2) NOT NULL,
    total_price DECIMAL(12,2) NOT NULL,

    PRIMARY KEY (order_item_id),

    KEY idx_order_items_order_id (order_id),
    KEY idx_order_items_product_id (product_id),

    CONSTRAINT fk_order_items_order
        FOREIGN KEY (order_id)
        REFERENCES orders(order_id)
        ON DELETE CASCADE
        ON UPDATE CASCADE,

    CONSTRAINT fk_order_items_product
        FOREIGN KEY (product_id)
        REFERENCES products(product_id)
        ON DELETE RESTRICT
        ON UPDATE CASCADE,

    CONSTRAINT chk_order_items_quantity
        CHECK (quantity > 0),

    CONSTRAINT chk_order_items_unit_price
        CHECK (unit_price >= 0),

    CONSTRAINT chk_order_items_total_price
        CHECK (total_price >= 0)
) ENGINE=InnoDB;


-- ==========================================================
-- ORDER LOGS
-- ==========================================================

CREATE TABLE IF NOT EXISTS order_logs (
    log_id BIGINT NOT NULL AUTO_INCREMENT,
    order_id INT NOT NULL,
    event_type VARCHAR(50) NOT NULL,
    old_status VARCHAR(50) NULL,
    new_status VARCHAR(50) NULL,
    message VARCHAR(500) NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (log_id),

    KEY idx_order_logs_order_id (order_id),

    CONSTRAINT fk_order_logs_order
        FOREIGN KEY (order_id)
        REFERENCES orders(order_id)
        ON DELETE CASCADE
        ON UPDATE CASCADE
) ENGINE=InnoDB;
