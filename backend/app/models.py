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
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
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
    purchase_date TIMESTAMPTZ DEFAULT now(),
    purchase_status VARCHAR(10) NOT NULL DEFAULT 'OPEN'
      CHECK (purchase_status IN ('OPEN','DONE')),
    updated_at TIMESTAMPTZ DEFAULT now(),
    FOREIGN KEY (customer_id) REFERENCES customers(customer_id)
);

-- รายการสินค้าที่ซื้อ
CREATE TABLE IF NOT EXISTS purchase_items (
    purchase_item_id SERIAL PRIMARY KEY,
    purchase_id INT,
    prod_id INT,
    weight DECIMAL(10, 2) CHECK (weight IS NULL OR weight >= 0),
    price DECIMAL(10, 2) CHECK (price IS NULL OR price >= 0),
    purchase_items_date TIMESTAMPTZ DEFAULT now(),
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
    payment_date TIMESTAMPTZ DEFAULT now(),
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
    sale_date TIMESTAMPTZ DEFAULT now(),
    FOREIGN KEY (prod_id) REFERENCES product(prod_id)
);

-- Unique Index กันชื่อหมวดหมู่ซ้ำ (ไม่แยกตัวพิมพ์)
CREATE UNIQUE INDEX IF NOT EXISTS ux_product_categories_lower_name
    ON product_categories (LOWER(category_name));

-- ตารางสรุปรวมน้ำหนักต่อสินค้า
CREATE TABLE IF NOT EXISTS product_inventory_totals (
    prod_id INT PRIMARY KEY REFERENCES product(prod_id) ON DELETE CASCADE,
    purchased_weight DECIMAL(14,2) NOT NULL DEFAULT 0,
    sold_weight      DECIMAL(14,2) NOT NULL DEFAULT 0
);
"""

def create_tables():
    with engine.begin() as conn:
        # 1) สร้างทุกตารางและดัชนีพื้นฐาน
        conn.exec_driver_sql(DDL)

        # 2) ทริกเกอร์อัปเดต updated_at อัตโนมัติเมื่อ UPDATE purchases
        conn.exec_driver_sql("""
        CREATE OR REPLACE FUNCTION trg_set_updated_at() RETURNS TRIGGER AS $$
        BEGIN
          NEW.updated_at = now();
          RETURN NEW;
        END; $$ LANGUAGE plpgsql;

        DROP TRIGGER IF EXISTS purchases_set_updated_at ON purchases;
        CREATE TRIGGER purchases_set_updated_at
        BEFORE UPDATE ON purchases
        FOR EACH ROW EXECUTE FUNCTION trg_set_updated_at();
        """)

        # 3) Unique partial index (ถ้าธรรมชาติธุรกิจต้องการจำกัด OPEN ได้ครั้งละ 1)
        conn.exec_driver_sql("""
        CREATE UNIQUE INDEX IF NOT EXISTS ux_purchases_only_one_open
          ON purchases ((1))
          WHERE purchase_status = 'OPEN';
        """)

        # 4) ทริกเกอร์รวมสต๊อกจาก purchase_items (รับเข้า)
        conn.exec_driver_sql("""
        CREATE OR REPLACE FUNCTION trg_upsert_totals_from_purchase_items()
        RETURNS TRIGGER AS $$
        BEGIN
          -- INSERT
          IF (TG_OP = 'INSERT') THEN
            INSERT INTO product_inventory_totals (prod_id, purchased_weight)
            VALUES (NEW.prod_id, COALESCE(NEW.weight, 0))
            ON CONFLICT (prod_id) DO UPDATE
              SET purchased_weight = product_inventory_totals.purchased_weight
                                    + COALESCE(EXCLUDED.purchased_weight,0);
            RETURN NEW;
          END IF;

          -- DELETE
          IF (TG_OP = 'DELETE') THEN
            INSERT INTO product_inventory_totals (prod_id, purchased_weight)
            VALUES (OLD.prod_id, -COALESCE(OLD.weight, 0))
            ON CONFLICT (prod_id) DO UPDATE
              SET purchased_weight = product_inventory_totals.purchased_weight
                                    + COALESCE(EXCLUDED.purchased_weight,0);
            RETURN OLD;
          END IF;

          -- UPDATE (อาจเปลี่ยน prod_id หรือ weight)
          IF (TG_OP = 'UPDATE') THEN
            IF (NEW.prod_id = OLD.prod_id) THEN
              INSERT INTO product_inventory_totals (prod_id, purchased_weight)
              VALUES (NEW.prod_id, COALESCE(NEW.weight,0) - COALESCE(OLD.weight,0))
              ON CONFLICT (prod_id) DO UPDATE
                SET purchased_weight = product_inventory_totals.purchased_weight
                                      + COALESCE(EXCLUDED.purchased_weight,0);
            ELSE
              -- ย้ายสินค้า: หักของเก่า เพิ่มของใหม่
              INSERT INTO product_inventory_totals (prod_id, purchased_weight)
              VALUES
                (OLD.prod_id, -COALESCE(OLD.weight,0)),
                (NEW.prod_id,  COALESCE(NEW.weight,0))
              ON CONFLICT (prod_id) DO UPDATE
                SET purchased_weight = product_inventory_totals.purchased_weight
                                      + COALESCE(EXCLUDED.purchased_weight,0);
            END IF;
            RETURN NEW;
          END IF;

          RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        DROP TRIGGER IF EXISTS purchase_items_totals_aiud ON purchase_items;
        CREATE TRIGGER purchase_items_totals_aiud
        AFTER INSERT OR UPDATE OR DELETE ON purchase_items
        FOR EACH ROW EXECUTE FUNCTION trg_upsert_totals_from_purchase_items();
        """)

        # 5) ทริกเกอร์รวมสต๊อกจาก stock_sales (ขายออก)
        conn.exec_driver_sql("""
        CREATE OR REPLACE FUNCTION trg_upsert_totals_from_stock_sales()
        RETURNS TRIGGER AS $$
        BEGIN
          -- INSERT
          IF (TG_OP = 'INSERT') THEN
            INSERT INTO product_inventory_totals (prod_id, sold_weight)
            VALUES (NEW.prod_id, COALESCE(NEW.weight_sold, 0))
            ON CONFLICT (prod_id) DO UPDATE
              SET sold_weight = product_inventory_totals.sold_weight
                               + COALESCE(EXCLUDED.sold_weight,0);
            RETURN NEW;
          END IF;

          -- DELETE
          IF (TG_OP = 'DELETE') THEN
            INSERT INTO product_inventory_totals (prod_id, sold_weight)
            VALUES (OLD.prod_id, -COALESCE(OLD.weight_sold, 0))
            ON CONFLICT (prod_id) DO UPDATE
              SET sold_weight = product_inventory_totals.sold_weight
                               + COALESCE(EXCLUDED.sold_weight,0);
            RETURN OLD;
          END IF;

          -- UPDATE (อาจเปลี่ยน prod_id หรือ weight_sold)
          IF (TG_OP = 'UPDATE') THEN
            IF (NEW.prod_id = OLD.prod_id) THEN
              INSERT INTO product_inventory_totals (prod_id, sold_weight)
              VALUES (NEW.prod_id, COALESCE(NEW.weight_sold,0) - COALESCE(OLD.weight_sold,0))
              ON CONFLICT (prod_id) DO UPDATE
                SET sold_weight = product_inventory_totals.sold_weight
                                 + COALESCE(EXCLUDED.sold_weight,0);
            ELSE
              INSERT INTO product_inventory_totals (prod_id, sold_weight)
              VALUES
                (OLD.prod_id, -COALESCE(OLD.weight_sold,0)),
                (NEW.prod_id,  COALESCE(NEW.weight_sold,0))
              ON CONFLICT (prod_id) DO UPDATE
                SET sold_weight = product_inventory_totals.sold_weight
                                 + COALESCE(EXCLUDED.sold_weight,0);
            END IF;
            RETURN NEW;
          END IF;

          RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        DROP TRIGGER IF EXISTS stock_sales_totals_aiud ON stock_sales;
        CREATE TRIGGER stock_sales_totals_aiud
        AFTER INSERT OR UPDATE OR DELETE ON stock_sales
        FOR EACH ROW EXECUTE FUNCTION trg_upsert_totals_from_stock_sales();
        """)

        # 6) Backfill ยอดเริ่มต้นจากข้อมูลเดิม (ถ้ามี)
        conn.exec_driver_sql("""
        INSERT INTO product_inventory_totals (prod_id, purchased_weight, sold_weight)
        SELECT
          p.prod_id,
          COALESCE((SELECT SUM(pi.weight) FROM purchase_items pi WHERE pi.prod_id = p.prod_id), 0),
          COALESCE((SELECT SUM(ss.weight_sold) FROM stock_sales ss WHERE ss.prod_id = p.prod_id), 0)
        FROM product p
        ON CONFLICT (prod_id) DO UPDATE
          SET purchased_weight = EXCLUDED.purchased_weight,
              sold_weight      = EXCLUDED.sold_weight;
        """)

