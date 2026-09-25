-- ==========================================================
-- CUSTOMERS
-- ==========================================================

CREATE TABLE IF NOT EXISTS customers (
    customer_id INT AUTO_INCREMENT PRIMARY KEY,

    email VARCHAR(255) NOT NULL UNIQUE,

    name VARCHAR(255) NOT NULL,

    token_hash VARCHAR(255) NOT NULL,

    role VARCHAR(30) NOT NULL DEFAULT 'USER',

    is_active BOOLEAN NOT NULL DEFAULT TRUE,

    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,

    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,

    INDEX idx_customers_active (is_active),

    INDEX idx_customers_role (role)
);


-- ==========================================================
-- PRODUCTS
-- ==========================================================

CREATE TABLE IF NOT EXISTS products (
    product_id INT AUTO_INCREMENT PRIMARY KEY,

    name VARCHAR(255) NOT NULL,

    description TEXT,

    price DECIMAL(12,2) NOT NULL,

    category VARCHAR(100) NOT NULL,

    is_active BOOLEAN NOT NULL DEFAULT TRUE,

    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,

    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,

    INDEX idx_products_category (category),

    INDEX idx_products_active (is_active)
);


-- ==========================================================
-- INVENTORY
-- ==========================================================

CREATE TABLE IF NOT EXISTS inventory (
    inventory_id BIGINT AUTO_INCREMENT PRIMARY KEY,

    product_id INT NOT NULL UNIQUE,

    quantity_available INT NOT NULL DEFAULT 0,

    reorder_threshold INT NOT NULL DEFAULT 10,

    last_updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,

    CONSTRAINT fk_inventory_product
        FOREIGN KEY (product_id)
        REFERENCES products(product_id)
);


-- ==========================================================
-- ORDERS
-- ==========================================================

CREATE TABLE IF NOT EXISTS orders (
    order_id INT AUTO_INCREMENT PRIMARY KEY,

    customer_id INT NOT NULL,

    order_number VARCHAR(50) NOT NULL UNIQUE,

    status VARCHAR(50) NOT NULL,

    total_amount DECIMAL(12,2) NOT NULL DEFAULT 0.00,

    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,

    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,

    CONSTRAINT fk_orders_customer
        FOREIGN KEY (customer_id)
        REFERENCES customers(customer_id)
);


-- ==========================================================
-- ORDER ITEMS
-- ==========================================================

CREATE TABLE IF NOT EXISTS order_items (
    order_item_id BIGINT AUTO_INCREMENT PRIMARY KEY,

    order_id INT NOT NULL,

    product_id INT NOT NULL,

    quantity INT NOT NULL,

    unit_price DECIMAL(12,2) NOT NULL,

    total_price DECIMAL(12,2) NOT NULL,

    CONSTRAINT fk_order_items_order
        FOREIGN KEY (order_id)
        REFERENCES orders(order_id),

    CONSTRAINT fk_order_items_product
        FOREIGN KEY (product_id)
        REFERENCES products(product_id)
);


-- ==========================================================
-- ORDER LOGS
-- ==========================================================

CREATE TABLE IF NOT EXISTS order_logs (
    log_id BIGINT AUTO_INCREMENT PRIMARY KEY,

    order_id INT NOT NULL,

    event_type VARCHAR(30) NOT NULL,

    old_status VARCHAR(50),

    new_status VARCHAR(50),

    message VARCHAR(500),

    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT fk_order_logs_order
        FOREIGN KEY (order_id)
        REFERENCES orders(order_id)
);


-- ==========================================================
-- SAMPLE CUSTOMERS
-- ==========================================================
--
-- Tokens:
--
-- ADMIN:
--   admin@123
--
-- PRODUCT OWNER:
--   product@123
--
-- USERS:
--   john@123
--   sarah@123
--   michael@123
--
-- Only SHA-256 hashes are stored in the database.
-- ==========================================================

INSERT INTO customers
    (email, name, token_hash, role, is_active)
VALUES
    (
        'admin@cloudmart.com',
        'Arjun Sharma',
        SHA2('admin@123', 256),
        'ADMIN',
        TRUE
    ),

    (
        'owner@cloudmart.com',
        'Priya Reddy',
        SHA2('product@123', 256),
        'PRODUCT_OWNER',
        TRUE
    ),

    (
        'john@cloudmart.com',
        'John Williams',
        SHA2('john@123', 256),
        'USER',
        TRUE
    ),

    (
        'sarah@cloudmart.com',
        'Sarah Johnson',
        SHA2('sarah@123', 256),
        'USER',
        TRUE
    ),

    (
        'michael@cloudmart.com',
        'Michael Brown',
        SHA2('michael@123', 256),
        'USER',
        TRUE
    )

ON DUPLICATE KEY UPDATE
    name = VALUES(name),
    role = VALUES(role),
    is_active = VALUES(is_active);


-- ==========================================================
-- SAMPLE PRODUCTS
-- ==========================================================

INSERT INTO products
    (name, description, price, category, is_active)
VALUES
    (
        'CloudMart Laptop',
        'High-performance business laptop',
        75000.00,
        'Electronics',
        TRUE
    ),

    (
        'Wireless Mouse',
        'Ergonomic wireless mouse',
        1200.00,
        'Accessories',
        TRUE
    ),

    (
        'Mechanical Keyboard',
        'RGB mechanical keyboard',
        4500.00,
        'Accessories',
        TRUE
    ),

    (
        'USB-C Hub',
        'Multi-port USB-C connectivity hub',
        2800.00,
        'Accessories',
        TRUE
    ),

    (
        '27-inch Monitor',
        'Full HD professional monitor',
        18000.00,
        'Electronics',
        TRUE
    )

ON DUPLICATE KEY UPDATE
    description = VALUES(description),
    price = VALUES(price),
    category = VALUES(category),
    is_active = VALUES(is_active);


-- ==========================================================
-- SAMPLE INVENTORY
-- ==========================================================

INSERT INTO inventory
    (product_id, quantity_available, reorder_threshold)
SELECT
    product_id,
    CASE name
        WHEN 'CloudMart Laptop' THEN 25
        WHEN 'Wireless Mouse' THEN 50
        WHEN 'Mechanical Keyboard' THEN 30
        WHEN 'USB-C Hub' THEN 8
        WHEN '27-inch Monitor' THEN 20
        ELSE 0
    END,

    CASE name
        WHEN 'CloudMart Laptop' THEN 10
        WHEN 'Wireless Mouse' THEN 15
        WHEN 'Mechanical Keyboard' THEN 10
        WHEN 'USB-C Hub' THEN 10
        WHEN '27-inch Monitor' THEN 5
        ELSE 10
    END

FROM products

WHERE name IN (
    'CloudMart Laptop',
    'Wireless Mouse',
    'Mechanical Keyboard',
    'USB-C Hub',
    '27-inch Monitor'
)

ON DUPLICATE KEY UPDATE
    quantity_available = VALUES(quantity_available),
    reorder_threshold = VALUES(reorder_threshold);