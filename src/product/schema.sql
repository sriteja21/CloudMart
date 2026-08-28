CREATE TABLE IF NOT EXISTS customers (
    customer_id INT AUTO_INCREMENT PRIMARY KEY,
    email VARCHAR(255) NOT NULL UNIQUE,
    name VARCHAR(255) NOT NULL,
    token_hash VARCHAR(255) NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP
);

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