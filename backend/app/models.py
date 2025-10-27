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
        # 1) สร้างตารางทั้งหมด
        conn.exec_driver_sql(DDL)

        # 2) updated_at อัตโนมัติเมื่อ UPDATE purchases
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

        # 3) (ตัวเลือก) จำกัด OPEN ได้ครั้งละ 1 บิล
        conn.exec_driver_sql("""
        CREATE UNIQUE INDEX IF NOT EXISTS ux_purchases_only_one_open
          ON purchases ((1))
          WHERE purchase_status = 'OPEN';
        """)

        # 4.1) ทริกเกอร์ purchase_items: นับเฉพาะเมื่อบิลเป็น DONE
        conn.exec_driver_sql("""
        CREATE OR REPLACE FUNCTION trg_upsert_totals_from_purchase_items_guarded()
        RETURNS TRIGGER AS $$
        DECLARE
          v_status_old TEXT;
          v_status_new TEXT;
        BEGIN
          IF TG_OP = 'INSERT' THEN
            SELECT purchase_status INTO v_status_new
            FROM purchases WHERE purchase_id = NEW.purchase_id;

            IF v_status_new = 'DONE' THEN
              INSERT INTO product_inventory_totals (prod_id, purchased_weight)
              VALUES (NEW.prod_id, COALESCE(NEW.weight,0))
              ON CONFLICT (prod_id) DO UPDATE
                SET purchased_weight = product_inventory_totals.purchased_weight
                                     + COALESCE(EXCLUDED.purchased_weight,0);
            END IF;
            RETURN NEW;

          ELSIF TG_OP = 'DELETE' THEN
            SELECT purchase_status INTO v_status_old
            FROM purchases WHERE purchase_id = OLD.purchase_id;

            IF v_status_old = 'DONE' THEN
              INSERT INTO product_inventory_totals (prod_id, purchased_weight)
              VALUES (OLD.prod_id, -COALESCE(OLD.weight,0))
              ON CONFLICT (prod_id) DO UPDATE
                SET purchased_weight = product_inventory_totals.purchased_weight
                                     + COALESCE(EXCLUDED.purchased_weight,0);
            END IF;
            RETURN OLD;

          ELSIF TG_OP = 'UPDATE' THEN
            IF NEW.purchase_id = OLD.purchase_id THEN
              SELECT purchase_status INTO v_status_new
              FROM purchases WHERE purchase_id = NEW.purchase_id;

              IF v_status_new = 'DONE' THEN
                IF NEW.prod_id = OLD.prod_id THEN
                  INSERT INTO product_inventory_totals (prod_id, purchased_weight)
                  VALUES (NEW.prod_id, COALESCE(NEW.weight,0) - COALESCE(OLD.weight,0))
                  ON CONFLICT (prod_id) DO UPDATE
                    SET purchased_weight = product_inventory_totals.purchased_weight
                                         + COALESCE(EXCLUDED.purchased_weight,0);
                ELSE
                  INSERT INTO product_inventory_totals (prod_id, purchased_weight)
                  VALUES
                    (OLD.prod_id, -COALESCE(OLD.weight,0)),
                    (NEW.prod_id,  COALESCE(NEW.weight,0))
                  ON CONFLICT (prod_id) DO UPDATE
                    SET purchased_weight = product_inventory_totals.purchased_weight
                                         + COALESCE(EXCLUDED.purchased_weight,0);
                END IF;
              END IF;
              RETURN NEW;

            ELSE
              -- ย้ายแถวไปอีกบิล
              SELECT purchase_status INTO v_status_old FROM purchases WHERE purchase_id = OLD.purchase_id;
              SELECT purchase_status INTO v_status_new FROM purchases WHERE purchase_id = NEW.purchase_id;

              IF v_status_old = 'DONE' THEN
                INSERT INTO product_inventory_totals (prod_id, purchased_weight)
                VALUES (OLD.prod_id, -COALESCE(OLD.weight,0))
                ON CONFLICT (prod_id) DO UPDATE
                  SET purchased_weight = product_inventory_totals.purchased_weight
                                       + COALESCE(EXCLUDED.purchased_weight,0);
              END IF;

              IF v_status_new = 'DONE' THEN
                INSERT INTO product_inventory_totals (prod_id, purchased_weight)
                VALUES (NEW.prod_id, COALESCE(NEW.weight,0))
                ON CONFLICT (prod_id) DO UPDATE
                  SET purchased_weight = product_inventory_totals.purchased_weight
                                       + COALESCE(EXCLUDED.purchased_weight,0);
              END IF;

              RETURN NEW;
            END IF;
          END IF;

          RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        DROP TRIGGER IF EXISTS purchase_items_totals_aiud ON purchase_items;
        CREATE TRIGGER purchase_items_totals_aiud
        AFTER INSERT OR UPDATE OR DELETE ON purchase_items
        FOR EACH ROW EXECUTE FUNCTION trg_upsert_totals_from_purchase_items_guarded();
        """)

        # 4.2) ทริกเกอร์ purchases: เมื่อ OPEN -> DONE ให้รวมยอดทั้งบิลเข้าสต๊อก
        conn.exec_driver_sql("""
        CREATE OR REPLACE FUNCTION trg_apply_totals_when_purchase_status_changes()
        RETURNS TRIGGER AS $$
        BEGIN
          IF (OLD.purchase_status <> 'DONE' AND NEW.purchase_status = 'DONE') THEN
            WITH agg AS (
              SELECT pi.prod_id, COALESCE(SUM(pi.weight),0) AS sum_w
              FROM purchase_items pi
              WHERE pi.purchase_id = NEW.purchase_id
              GROUP BY pi.prod_id
            )
            INSERT INTO product_inventory_totals (prod_id, purchased_weight, sold_weight)
            SELECT a.prod_id, a.sum_w, 0
            FROM agg a
            ON CONFLICT (prod_id) DO UPDATE
              SET purchased_weight = product_inventory_totals.purchased_weight
                                   + EXCLUDED.purchased_weight;

          ELSIF (OLD.purchase_status = 'DONE' AND NEW.purchase_status <> 'DONE') THEN
            -- เผื่อมีกรณี reopen (ถ้าไม่ใช้เส้นทางนี้ จะไม่ถูกเรียก)
            WITH agg AS (
              SELECT pi.prod_id, COALESCE(SUM(pi.weight),0) AS sum_w
              FROM purchase_items pi
              WHERE pi.purchase_id = NEW.purchase_id
              GROUP BY pi.prod_id
            )
            INSERT INTO product_inventory_totals (prod_id, purchased_weight, sold_weight)
            SELECT a.prod_id, -a.sum_w, 0
            FROM agg a
            ON CONFLICT (prod_id) DO UPDATE
              SET purchased_weight = product_inventory_totals.purchased_weight
                                   + EXCLUDED.purchased_weight;
          END IF;

          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        DROP TRIGGER IF EXISTS purchases_status_apply ON purchases;
        CREATE TRIGGER purchases_status_apply
        AFTER UPDATE OF purchase_status ON purchases
        FOR EACH ROW EXECUTE FUNCTION trg_apply_totals_when_purchase_status_changes();
        """)

        # 5) ทริกเกอร์ขายออก: คงเดิม
        conn.exec_driver_sql("""
        CREATE OR REPLACE FUNCTION trg_upsert_totals_from_stock_sales()
        RETURNS TRIGGER AS $$
        BEGIN
          IF (TG_OP = 'INSERT') THEN
            INSERT INTO product_inventory_totals (prod_id, sold_weight)
            VALUES (NEW.prod_id, COALESCE(NEW.weight_sold, 0))
            ON CONFLICT (prod_id) DO UPDATE
              SET sold_weight = product_inventory_totals.sold_weight
                               + COALESCE(EXCLUDED.sold_weight,0);
            RETURN NEW;
          ELSIF (TG_OP = 'DELETE') THEN
            INSERT INTO product_inventory_totals (prod_id, sold_weight)
            VALUES (OLD.prod_id, -COALESCE(OLD.weight_sold, 0))
            ON CONFLICT (prod_id) DO UPDATE
              SET sold_weight = product_inventory_totals.sold_weight
                               + COALESCE(EXCLUDED.sold_weight,0);
            RETURN OLD;
          ELSIF (TG_OP = 'UPDATE') THEN
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

        # 6) Backfill ให้ถูกต้อง: นับเฉพาะบิล DONE
        conn.exec_driver_sql("""
        INSERT INTO product_inventory_totals (prod_id, purchased_weight, sold_weight)
        SELECT
          p.prod_id,
          COALESCE((
            SELECT SUM(pi.weight)
            FROM purchase_items pi
            JOIN purchases pu ON pu.purchase_id = pi.purchase_id
            WHERE pi.prod_id = p.prod_id
              AND pu.purchase_status = 'DONE'
          ), 0),
          COALESCE((
            SELECT SUM(ss.weight_sold)
            FROM stock_sales ss
            WHERE ss.prod_id = p.prod_id
          ), 0)
        FROM product p
        ON CONFLICT (prod_id) DO UPDATE
          SET purchased_weight = EXCLUDED.purchased_weight,
              sold_weight      = EXCLUDED.sold_weight;
        """)
