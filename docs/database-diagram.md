# CloudMart Database Diagram

Engine: MySQL 8.0 on RDS (`db.t3.micro`, private subnets). Schema source: `src/init-schema/schema.sql`.

```mermaid
erDiagram
    CUSTOMERS ||--o{ ORDERS : places
    ORDERS ||--|{ ORDER_ITEMS : contains
    ORDERS ||--o{ ORDER_LOGS : "audit trail"
    PRODUCTS ||--o{ ORDER_ITEMS : "ordered as"
    PRODUCTS ||--o| INVENTORY : "stocked as"

    CUSTOMERS {
        int customer_id PK "AUTO_INCREMENT"
        varchar255 email UK "NOT NULL"
        varchar255 name "NOT NULL"
        varchar255 token_hash "SHA-256 of bearer token"
        varchar30 role "USER | PRODUCT_OWNER | ADMIN"
        boolean is_active "DEFAULT TRUE"
        datetime created_at
        datetime updated_at
    }

    PRODUCTS {
        int product_id PK "AUTO_INCREMENT"
        varchar255 name "NOT NULL"
        text description
        decimal12_2 price "NOT NULL"
        varchar100 category "NOT NULL"
        boolean is_active "DEFAULT TRUE (soft delete)"
        datetime created_at
        datetime updated_at
    }

    INVENTORY {
        bigint inventory_id PK "AUTO_INCREMENT"
        int product_id FK,UK "1:1 with products"
        int quantity_available "DEFAULT 0"
        int reorder_threshold "DEFAULT 10"
        datetime last_updated_at
    }

    ORDERS {
        int order_id PK "AUTO_INCREMENT"
        int customer_id FK
        varchar50 order_number UK "ORD-000001"
        varchar50 status "PENDING, CONFIRMED, ..."
        decimal12_2 total_amount "DEFAULT 0.00"
        datetime created_at
        datetime updated_at
    }

    ORDER_ITEMS {
        bigint order_item_id PK "AUTO_INCREMENT"
        int order_id FK
        int product_id FK
        int quantity
        decimal12_2 unit_price "price snapshot"
        decimal12_2 total_price
    }

    ORDER_LOGS {
        bigint log_id PK "AUTO_INCREMENT"
        int order_id FK
        varchar30 event_type "CREATED, STATUS_CHANGED, CANCELLED"
        varchar50 old_status
        varchar50 new_status
        varchar500 message
        datetime created_at
    }
```

## Relationships

| Parent | Child | Cardinality | FK constraint |
|---|---|---|---|
| customers | orders | 1 : N | `fk_orders_customer` |
| orders | order_items | 1 : N | `fk_order_items_order` |
| products | order_items | 1 : N | `fk_order_items_product` |
| products | inventory | 1 : 0..1 | `fk_inventory_product` (+ UNIQUE on `product_id`) |
| orders | order_logs | 1 : N | `fk_order_logs_order` |


## Indexes

- `customers`: `idx_customers_active (is_active)`, `idx_customers_role (role)`, unique `email`
- `products`: `idx_products_category (category)`, `idx_products_active (is_active)`
- `orders`: unique `order_number`
- `inventory`: unique `product_id`
