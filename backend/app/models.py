from sqlalchemy import text
from .database import engine

DDL = """
-- ตารางหมวดหมู่สินค้า
CREATE TABLE IF NOT EXISTS product_categories (
   category_id SERIAL PRIMARY KEY,
   category_name VARCHAR(255) NOT NULL
);

-- ตารางสินค้า
CREATE TABLE IF NOT EXISTS product (
    prod_id SERIAL PRIMARY KEY,
    prod_img TEXT,
    prod_name VARCHAR(255),
    prod_price DECIMAL(10, 2) CHECK (prod_price IS NULL OR prod_price >= 0),
    category_id INT,
    FOREIGN KEY (category_id) REFERENCES product_categories(category_id)
);

-- ตารางลูกค้า
CREATE TABLE IF NOT EXISTS customers (
    customer_id SERIAL PRIMARY KEY,
    full_name VARCHAR(255),
    national_id VARCHAR(20) UNIQUE,
    address TEXT
);

-- ตารางรูปภาพของลูกค้า
CREATE TABLE IF NOT EXISTS customer_photos (
    photo_id SERIAL PRIMARY KEY,
    customer_id INT,
    photo_path TEXT,
    FOREIGN KEY (customer_id) REFERENCES customers(customer_id)
);

-- ตารางการซื้อ
CREATE TABLE IF NOT EXISTS purchases (
    purchase_id SERIAL PRIMARY KEY,
    customer_id INT,
    purchase_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    purchase_status VARCHAR(10) NOT NULL DEFAULT 'OPEN'
      CHECK (purchase_status IN ('OPEN','PAID','VOID')),
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (customer_id) REFERENCES customers(customer_id)
);

-- รายการสินค้าที่ซื้อ
CREATE TABLE IF NOT EXISTS purchase_items (
    purchase_item_id SERIAL PRIMARY KEY,
    purchase_id INT,
    prod_id INT,
    weight DECIMAL(10, 2) CHECK (weight IS NULL OR weight >= 0),
    price DECIMAL(10, 2) CHECK (price IS NULL OR price >= 0),
    FOREIGN KEY (purchase_id) REFERENCES purchases(purchase_id) ON DELETE CASCADE,
    FOREIGN KEY (prod_id) REFERENCES product(prod_id) ON DELETE CASCADE
);

-- รูปภาพในแต่ละรายการสินค้าที่ซื้อ
CREATE TABLE IF NOT EXISTS purchase_item_photos (
    photo_id SERIAL PRIMARY KEY,
    purchase_item_id INT,
    img_path TEXT,
    FOREIGN KEY (purchase_item_id) REFERENCES purchase_items(purchase_item_id) ON DELETE CASCADE
);

-- การชำระเงิน
CREATE TABLE IF NOT EXISTS payment (
    payment_id SERIAL PRIMARY KEY,
    purchase_id INT,
    payment_method VARCHAR(50) CHECK (payment_method IN ('เงินสด', 'เงินโอน')),
    payment_amount DECIMAL(10, 2) CHECK (payment_amount IS NULL OR payment_amount >= 0),
    payment_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (purchase_id) REFERENCES purchases(purchase_id)
);

-- รูปภาพหลักฐานการชำระเงิน
CREATE TABLE IF NOT EXISTS payment_photo (
    photo_id SERIAL PRIMARY KEY,
    payment_id INT,
    payment_img TEXT,
    FOREIGN KEY (payment_id) REFERENCES payment(payment_id)
);

-- การขายสินค้าจากสต๊อก
CREATE TABLE IF NOT EXISTS stock_sales (
    stock_sales_id SERIAL PRIMARY KEY,
    prod_id INT,
    weight_sold DECIMAL(10,2) CHECK (weight_sold IS NULL OR weight_sold >= 0),
    sale_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (prod_id) REFERENCES product(prod_id)
);

-- Unique Index กันชื่อหมวดหมู่ซ้ำ (ไม่แยกตัวพิมพ์)
CREATE UNIQUE INDEX IF NOT EXISTS ux_product_categories_lower_name
    ON product_categories (LOWER(category_name));
"""

def create_tables():
    with engine.begin() as conn:
        conn.exec_driver_sql(DDL)
        # ทริกเกอร์อัปเดต updated_at อัตโนมัติเมื่อมีการ UPDATE
        conn.exec_driver_sql("""
        CREATE OR REPLACE FUNCTION trg_set_updated_at() RETURNS TRIGGER AS $$
        BEGIN
          NEW.updated_at = CURRENT_TIMESTAMP;
          RETURN NEW;
        END; $$ LANGUAGE plpgsql;

        DROP TRIGGER IF EXISTS purchases_set_updated_at ON purchases;
        CREATE TRIGGER purchases_set_updated_at
        BEFORE UPDATE ON purchases
        FOR EACH ROW EXECUTE FUNCTION trg_set_updated_at();
        """)
        conn.exec_driver_sql("""
        CREATE UNIQUE INDEX IF NOT EXISTS ux_purchases_only_one_open
          ON purchases ((1))
          WHERE purchase_status = 'OPEN';
        """)