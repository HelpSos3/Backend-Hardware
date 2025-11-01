from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from typing import Optional, List, Literal, Dict, Any
from sqlalchemy import text
from sqlalchemy.orm import Session
from app.database import SessionLocal
from io import BytesIO
from datetime import datetime
from openpyxl import Workbook

from urllib.parse import quote
router = APIRouter(prefix="/inventory", tags=["inventory"])

# ----------- DB Session -----------
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def _parse_prod_ids_csv(s: Optional[str]) -> Optional[List[int]]:
    if not s:
        return None
    ids = []
    for tok in s.split(","):
        tok = tok.strip()
        if tok:
            try:
                ids.append(int(tok))
            except ValueError:
                pass
    return ids or None

def _make_date_to_exclusive(date_to: Optional[datetime]) -> Optional[datetime]:
    if not date_to:
        return None
    from datetime import timedelta
    dt0 = date_to.replace(hour=0, minute=0, second=0, microsecond=0)
    return dt0 + timedelta(days=1)

# ----------- Models -----------
class InventoryItem(BaseModel):
    prod_id: int
    prod_code: str
    prod_name: Optional[str]
    prod_img: Optional[str]
    category: Optional[Dict[str, Any]]
    last_sale_date: Optional[datetime]
    last_sold_qty: Optional[float] = None
    balance_weight: float = 0.0

class InventoryListResponse(BaseModel):
    items: List[InventoryItem]
    page: int
    per_page: int
    total: int

class SellLine(BaseModel):
    prod_id: int = Field(..., ge=1)
    weight_sold: float = Field(..., gt=0)
    note: Optional[str] = None  

class SellBulkResult(BaseModel):
    ok: bool
    created: List[Dict[str, Any]]

class HistoryRow(BaseModel):
    prod_id: int
    prod_name: Optional[str]
    weight: float
    price: Optional[float] = None
    date: datetime

class HistoryListResponse(BaseModel):
    items: List[HistoryRow]
    page: int
    per_page: int
    total: int    

SortKey = Literal["last_sale_date","-last_sale_date","name","-name","balance","-balance"]

# ----------- GET /inventory/items -----------
@router.get("/items", response_model=InventoryListResponse)
def list_inventory_items(
    category_id: Optional[int] = Query(None),
    q: Optional[str] = Query(None, description="ค้นชื่อสินค้า หรือ #รหัส (#001) หรือ prod_id"),
    only_active: bool = Query(True),
    sort: SortKey = Query("-balance"),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
):
    offset = (page - 1) * per_page

    sql = text("""
          WITH latest_sale AS (
            SELECT
              ss.prod_id,
              ss.weight_sold,
              ss.sale_date,
              ROW_NUMBER() OVER (
                PARTITION BY ss.prod_id
                ORDER BY ss.sale_date DESC, ss.stock_sales_id DESC
              ) AS rn
            FROM stock_sales ss
          ),
          agg_latest AS (
            SELECT
              prod_id,
              sale_date  AS last_sale_date,
              weight_sold AS last_sold_qty
            FROM latest_sale
            WHERE rn = 1
          )
          SELECT
            p.prod_id,
            p.prod_name,
            p.prod_img,
            pc.category_id,
            pc.category_name,
            (COALESCE(pit.purchased_weight,0) - COALESCE(pit.sold_weight,0)) AS balance_weight,
            al.last_sale_date,
            al.last_sold_qty
          FROM product p
          LEFT JOIN product_categories pc ON pc.category_id = p.category_id
          JOIN product_inventory_totals pit ON pit.prod_id = p.prod_id
          LEFT JOIN agg_latest al ON al.prod_id = p.prod_id
          WHERE (:only_active = FALSE OR p.is_active = TRUE)
            AND (:category_id IS NULL OR p.category_id = :category_id)
            AND (
                :q IS NULL OR :q = ''
              OR  p.prod_name ILIKE '%'||:q||'%'
              OR  ('#' || LPAD(p.prod_id::text, 3, '0')) ILIKE '%'||:q||'%'
              OR  p.prod_id::text = :q
            )
          ORDER BY
            CASE WHEN :sort='-last_sale_date' THEN al.last_sale_date END DESC,
            CASE WHEN :sort='last_sale_date'  THEN al.last_sale_date END ASC,
            CASE WHEN :sort='-balance'        THEN (COALESCE(pit.purchased_weight,0)-COALESCE(pit.sold_weight,0)) END DESC,
            CASE WHEN :sort='balance'         THEN (COALESCE(pit.purchased_weight,0)-COALESCE(pit.sold_weight,0)) END ASC,
            CASE WHEN :sort='-name'           THEN p.prod_name END DESC,
            CASE WHEN :sort='name'            THEN p.prod_name END ASC,
            p.prod_id ASC
          LIMIT :limit OFFSET :offset;
          """)

    sql_count = text("""
      SELECT COUNT(*)
      FROM product p
      WHERE (:only_active = FALSE OR p.is_active = TRUE)
        AND (:category_id IS NULL OR p.category_id = :category_id)
        AND (
             :q IS NULL OR :q = ''
          OR  p.prod_name ILIKE '%'||:q||'%'
          OR  ('#' || LPAD(p.prod_id::text, 3, '0')) ILIKE '%'||:q||'%'
          OR  p.prod_id::text = :q
        )
    """)

    rows = db.execute(sql, {
        "category_id": category_id,
        "q": q,
        "only_active": only_active,
        "sort": sort,
        "limit": per_page,
        "offset": offset
    }).mappings().all()

    total = db.execute(sql_count, {
        "category_id": category_id,
        "q": q,
        "only_active": only_active
    }).scalar() or 0

    items: List[InventoryItem] = []
    for r in rows:
        items.append(InventoryItem(
            prod_id=r["prod_id"],
            prod_code=f'#{str(r["prod_id"]).zfill(3)}',
            prod_name=r["prod_name"],
            prod_img=r["prod_img"],
            category={"id": r["category_id"], "name": r["category_name"]} if r["category_id"] is not None else None,
            last_sale_date=r["last_sale_date"],
            last_sold_qty=float(r["last_sold_qty"]) if r["last_sold_qty"] is not None else None,
            balance_weight=float(r["balance_weight"]) if r["balance_weight"] is not None else 0.0
        ))

    return InventoryListResponse(items=items, page=page, per_page=per_page, total=total)

# ----------- POST /stock_sales/bulk -----------
@router.post("/sell", response_model=SellBulkResult)
def sell_bulk(lines: List[SellLine], db: Session = Depends(get_db)):
    if not lines:
        raise HTTPException(status_code=400, detail="no items")

    created = []
    try:
        with db.begin():
            prod_ids = {line.prod_id for line in lines}

            # 0) ตรวจว่าสินค้ามีอยู่จริงทุกตัว
            exist_sql = text("SELECT prod_id FROM product WHERE prod_id = ANY(:ids)")
            exist_set = {r["prod_id"] for r in db.execute(exist_sql, {"ids": list(prod_ids)}).mappings().all()}
            missing = prod_ids - exist_set
            if missing:
                raise HTTPException(status_code=404, detail=f"product not found: {sorted(missing)}")

            # 1) ensure totals row (กรณีมีสินค้าที่เพิ่งสร้างแต่ยังไม่เคยมีการซื้อ/ขาย)
            ensure_sql = text("""
                INSERT INTO product_inventory_totals (prod_id, purchased_weight, sold_weight)
                SELECT p.prod_id, 0, 0
                FROM product p
                LEFT JOIN product_inventory_totals pit ON pit.prod_id = p.prod_id
                WHERE p.prod_id = ANY(:ids) AND pit.prod_id IS NULL
                ON CONFLICT (prod_id) DO NOTHING
            """)
            db.execute(ensure_sql, {"ids": list(prod_ids)})

            # 2) ล็อกแถว totals ที่เกี่ยวข้อง
            lock_sql = text("""
                SELECT prod_id, purchased_weight, sold_weight
                FROM product_inventory_totals
                WHERE prod_id = ANY(:ids)
                FOR UPDATE
            """)
            db.execute(lock_sql, {"ids": list(prod_ids)})

            # 3) อ่านคงเหลือแบบคำนวณ (ภายใต้ FOR UPDATE)
            cur_sql = text("""
                SELECT pit.prod_id,
                       (COALESCE(pit.purchased_weight,0) - COALESCE(pit.sold_weight,0)) AS balance_weight
                FROM product_inventory_totals pit
                WHERE pit.prod_id = ANY(:ids)
                FOR UPDATE
            """)
            balances = {
                r["prod_id"]: float(r["balance_weight"])
                for r in db.execute(cur_sql, {"ids": list(prod_ids)}).mappings().all()
            }

            # 4) รวมยอด/ตรวจไม่ให้ติดลบ
            aggregate: Dict[int, float] = {}
            for line in lines:
                aggregate[line.prod_id] = aggregate.get(line.prod_id, 0.0) + float(line.weight_sold)

            for pid, qty in aggregate.items():
                if qty <= 0:
                    raise HTTPException(status_code=400, detail=f"weight_sold must be > 0 for product {pid}")
                if qty > balances.get(pid, 0.0):
                    raise HTTPException(status_code=409, detail=f"insufficient balance for product {pid}: {qty} > {balances.get(pid, 0.0)}")

            # 5) แทรกขายออก (trigger จะอัพเดต totals ให้เอง)
            insert_sql = text("""
                INSERT INTO stock_sales (prod_id, weight_sold)
                VALUES (:pid, :qty)
                RETURNING stock_sales_id, sale_date
            """)
            for line in lines:
                row = db.execute(insert_sql, {"pid": line.prod_id, "qty": float(line.weight_sold)}).mappings().first()
                created.append({
                    "prod_id": line.prod_id,
                    "weight_sold": float(line.weight_sold),
                    "stock_sales_id": row["stock_sales_id"],
                    "sale_date": row["sale_date"].isoformat() if row["sale_date"] else None
                })

        return SellBulkResult(ok=True, created=created)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
# ----------- GET /inventory/export -----------
@router.get("/export")
def export_inventory_excel(
    category_id: Optional[int] = Query(None),
    q: Optional[str] = Query(None),
    only_active: bool = Query(True),
    sort: SortKey = Query("-last_sale_date"),
    db: Session = Depends(get_db),
):
    sql = """
WITH latest_sale AS (
  SELECT
    ss.prod_id,
    ss.weight_sold,
    ss.sale_date,
    ROW_NUMBER() OVER (
      PARTITION BY ss.prod_id
      ORDER BY ss.sale_date DESC, ss.stock_sales_id DESC
    ) AS rn
  FROM stock_sales ss
),
agg_latest AS (
  SELECT
    prod_id,
    sale_date   AS last_sale_date,
    weight_sold AS last_sold_qty
  FROM latest_sale
  WHERE rn = 1
)
SELECT
  p.prod_id,
  p.prod_name,
  p.prod_img,
  pc.category_id,
  pc.category_name,
  (COALESCE(pit.purchased_weight,0) - COALESCE(pit.sold_weight,0)) AS balance_weight,
  al.last_sale_date,
  al.last_sold_qty
FROM product p
LEFT JOIN product_categories pc ON pc.category_id = p.category_id
LEFT JOIN product_inventory_totals pit ON pit.prod_id = p.prod_id
LEFT JOIN agg_latest al ON al.prod_id = p.prod_id      -- << เปลี่ยนจาก JOIN เป็น LEFT JOIN
WHERE (:only_active = FALSE OR p.is_active = TRUE)
  AND (:category_id IS NULL OR p.category_id = :category_id)
  AND (
       :q IS NULL OR :q = ''
    OR  p.prod_name ILIKE '%'||:q||'%'
    OR  ('#' || LPAD(p.prod_id::text, 3, '0')) ILIKE '%'||:q||'%'
    OR  p.prod_id::text = :q
  )
ORDER BY
  CASE WHEN :sort='-last_sale_date' THEN al.last_sale_date END DESC NULLS LAST,
  CASE WHEN :sort='last_sale_date'  THEN al.last_sale_date END ASC  NULLS FIRST,
  CASE WHEN :sort='-balance'        THEN (COALESCE(pit.purchased_weight,0)-COALESCE(pit.sold_weight,0)) END DESC,
  CASE WHEN :sort='balance'         THEN (COALESCE(pit.purchased_weight,0)-COALESCE(pit.sold_weight,0)) END ASC,
  CASE WHEN :sort='-name'           THEN p.prod_name END DESC,
  CASE WHEN :sort='name'            THEN p.prod_name END ASC,
  p.prod_id ASC
"""
    rows = db.execute(text(sql), {
        "category_id": category_id,
        "q": q,
        "only_active": only_active,
        "sort": sort
    }).mappings().all()

    wb = Workbook()
    ws = wb.active
    ws.title = "รายงานคลังสินค้า"

    headers = ["รหัส", "ชื่อสินค้า", "หมวดหมู่", "คงเหลือ (kg)", "วันที่ขายล่าสุด", "จำนวนที่ขายล่าสุด (kg)"]
    ws.append(headers)

    for r in rows:
        prod_code = f'#{str(r["prod_id"]).zfill(3)}'
        last_date = r["last_sale_date"].strftime("%Y-%m-%d %H:%M") if r["last_sale_date"] else ""
        ws.append([
            prod_code,
            r["prod_name"] or "",
            r["category_name"] or "",
            float(r["balance_weight"]) if r["balance_weight"] is not None else 0.0,
            last_date,
            float(r["last_sold_qty"]) if r["last_sold_qty"] is not None else ""
        ])

    from io import BytesIO
    output = BytesIO()
    wb.save(output)
    output.seek(0)

    from urllib.parse import quote
    filename = "รายงานคลังสินค้า.xlsx"
    encoded_name = quote(filename)
    headers = {"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_name}"}

    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers=headers,
    )

@router.get("/purchased_history_simple/{prod_id}", response_model=HistoryListResponse)
def get_purchased_history_simple(
    prod_id: int,
    date_from: Optional[datetime] = Query(None, description="เริ่มวันที่ (ISO)"),
    date_to: Optional[datetime] = Query(None, description="สิ้นสุดวันที่ (ISO)"),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
):
    offset = (page - 1) * per_page

    # ทำให้ date_to ครอบคลุมทั้งวันแบบ half-open [from, to)
    from datetime import timedelta
    date_to_exclusive = None
    if date_to is not None:
        dt0 = date_to.replace(hour=0, minute=0, second=0, microsecond=0)
        date_to_exclusive = dt0 + timedelta(days=1)

    list_sql = text("""
        SELECT
          pi.prod_id,
          p.prod_name,
          pi.weight,
          pi.price,
          pi.purchase_items_date AS date
        FROM purchase_items pi
        LEFT JOIN product p ON p.prod_id = pi.prod_id
        WHERE pi.prod_id = :pid
          AND (:date_from IS NULL OR pi.purchase_items_date >= :date_from)
          AND (:date_to_exclusive IS NULL OR pi.purchase_items_date < :date_to_exclusive)
        ORDER BY pi.purchase_items_date DESC, pi.purchase_item_id DESC
        LIMIT :limit OFFSET :offset
    """)

    count_sql = text("""
        SELECT COUNT(*)
        FROM purchase_items pi
        WHERE pi.prod_id = :pid
          AND (:date_from IS NULL OR pi.purchase_items_date >= :date_from)
          AND (:date_to_exclusive IS NULL OR pi.purchase_items_date < :date_to_exclusive)
    """)

    params = {
        "pid": prod_id,
        "date_from": date_from,
        "date_to_exclusive": date_to_exclusive,
        "limit": per_page,
        "offset": offset,
    }

    rows = db.execute(list_sql, params).mappings().all()
    total = db.execute(count_sql, params).scalar() or 0

    items = [
        HistoryRow(
            prod_id=r["prod_id"],
            prod_name=r["prod_name"],
            weight=float(r["weight"]) if r["weight"] is not None else 0.0,
            price=float(r["price"]) if r["price"] is not None else None,
            date=r["date"],
        )
        for r in rows
    ]
    return HistoryListResponse(items=items, page=page, per_page=per_page, total=total)


@router.get("/sold_history_simple/{prod_id}", response_model=HistoryListResponse)
def get_sold_history_simple(
    prod_id: int,
    date_from: Optional[datetime] = Query(None, description="เริ่มวันที่ (ISO)"),
    date_to: Optional[datetime] = Query(None, description="สิ้นสุดวันที่ (ISO) (รวมทั้งวัน)"),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
):
    offset = (page - 1) * per_page

    # ทำให้ date_to ครอบคลุมทั้งวันแบบ half-open [from, to)
    from datetime import timedelta
    date_to_exclusive = None
    if date_to is not None:
        dt0 = date_to.replace(hour=0, minute=0, second=0, microsecond=0)
        date_to_exclusive = dt0 + timedelta(days=1)

    list_sql = text("""
        SELECT
          ss.prod_id,
          p.prod_name,
          ss.weight_sold AS weight,
          ss.sale_date    AS date
        FROM stock_sales ss
        LEFT JOIN product p ON p.prod_id = ss.prod_id
        WHERE ss.prod_id = :pid
          AND (:date_from IS NULL OR ss.sale_date >= :date_from)
          AND (:date_to_exclusive IS NULL OR ss.sale_date < :date_to_exclusive)
        ORDER BY ss.sale_date DESC, ss.stock_sales_id DESC
        LIMIT :limit OFFSET :offset
    """)

    count_sql = text("""
        SELECT COUNT(*)
        FROM stock_sales ss
        WHERE ss.prod_id = :pid
          AND (:date_from IS NULL OR ss.sale_date >= :date_from)
          AND (:date_to_exclusive IS NULL OR ss.sale_date < :date_to_exclusive)
    """)

    params = {
        "pid": prod_id,
        "date_from": date_from,
        "date_to_exclusive": date_to_exclusive,
        "limit": per_page,
        "offset": offset,
    }

    rows = db.execute(list_sql, params).mappings().all()
    total = db.execute(count_sql, params).scalar() or 0

    items = [
        HistoryRow(
            prod_id=r["prod_id"],
            prod_name=r["prod_name"],
            weight=float(r["weight"]),
            date=r["date"],
        )
        for r in rows
    ]
    return HistoryListResponse(items=items, page=page, per_page=per_page, total=total)

@router.get("/export_purchased")
def export_purchased_excel(
    prod_ids: Optional[str] = Query(None, description="เช่น 1,2,5 ไม่ส่ง = ทุกสินค้า"),
    category_id: Optional[int] = Query(None),
    q: Optional[str] = Query(None),
    only_active: bool = Query(True),
    date_from: Optional[datetime] = Query(None),
    date_to: Optional[datetime] = Query(None),
    db: Session = Depends(get_db),
):
    ids_list = _parse_prod_ids_csv(prod_ids)
    date_to_exclusive = _make_date_to_exclusive(date_to)

    sql = text("""
WITH filtered_product AS (
  SELECT p.prod_id, p.prod_name, p.prod_img, p.category_id
  FROM product p
  WHERE (:only_active = FALSE OR p.is_active = TRUE)
    AND (:category_id IS NULL OR p.category_id = :category_id)
    AND (
         :q IS NULL OR :q = ''
      OR  p.prod_name ILIKE '%'||:q||'%'
      OR  ('#' || LPAD(p.prod_id::text, 3, '0')) ILIKE '%'||:q||'%'
      OR  p.prod_id::text = :q
    )
    AND (:ids_is_null OR p.prod_id = ANY(:ids))
)
SELECT
  fp.prod_id,
  fp.prod_name,
  pc.category_name,
  pi.weight,
  pi.price,
  pi.purchase_items_date AS dt
FROM purchase_items pi
JOIN filtered_product fp ON fp.prod_id = pi.prod_id
LEFT JOIN product_categories pc ON pc.category_id = fp.category_id
WHERE (:date_from IS NULL OR pi.purchase_items_date >= :date_from)
  AND (:date_to_ex IS NULL OR pi.purchase_items_date < :date_to_ex)
ORDER BY pi.purchase_items_date DESC, pi.purchase_item_id DESC
""")

    rows = db.execute(sql, {
        "only_active": only_active,
        "category_id": category_id,
        "q": q,
        "ids_is_null": ids_list is None,
        "ids": ids_list or [],
        "date_from": date_from,
        "date_to_ex": date_to_exclusive,
    }).mappings().all()

    # สร้าง Excel
    wb = Workbook()
    ws = wb.active
    ws.title = "รับซื้อทั้งหมด"

    headers = ["รหัส", "ชื่อสินค้า", "หมวดหมู่", "น้ำหนัก (kg)", "ราคา", "วันที่รับซื้อ"]
    ws.append(headers)

    for r in rows:
        prod_code = f'#{str(r["prod_id"]).zfill(3)}'
        dt = r["dt"].strftime("%Y-%m-%d %H:%M") if r["dt"] else ""
        ws.append([
            prod_code,
            r["prod_name"] or "",
            r["category_name"] or "",
            float(r["weight"]) if r["weight"] is not None else 0.0,
            float(r["price"]) if r["price"] is not None else "",
            dt
        ])

    # ปรับความกว้างคอลัมน์
    for col in ws.columns:
        max_len = 0
        col_letter = col[0].column_letter
        for cell in col:
            max_len = max(max_len, len(str(cell.value)) if cell.value is not None else 0)
        ws.column_dimensions[col_letter].width = max_len + 2

    output = BytesIO()
    wb.save(output)
    output.seek(0)

    filename = "รายงานรับซื้อทั้งหมด.xlsx"
    encoded_name = quote(filename)
    headers = {"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_name}"}

    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers=headers,
    )

# ----------- GET /inventory/export_sold -----------
@router.get("/export_sold")
def export_sold_excel(
    prod_ids: Optional[str] = Query(None, description="เช่น 1,2,5 ไม่ส่ง = ทุกสินค้า"),
    category_id: Optional[int] = Query(None),
    q: Optional[str] = Query(None),
    only_active: bool = Query(True),
    date_from: Optional[datetime] = Query(None),
    date_to: Optional[datetime] = Query(None),
    db: Session = Depends(get_db),
):
    ids_list = _parse_prod_ids_csv(prod_ids)
    date_to_exclusive = _make_date_to_exclusive(date_to)

    sql = text("""
WITH filtered_product AS (
  SELECT p.prod_id, p.prod_name, p.prod_img, p.category_id
  FROM product p
  WHERE (:only_active = FALSE OR p.is_active = TRUE)
    AND (:category_id IS NULL OR p.category_id = :category_id)
    AND (
         :q IS NULL OR :q = ''
      OR  p.prod_name ILIKE '%'||:q||'%'
      OR  ('#' || LPAD(p.prod_id::text, 3, '0')) ILIKE '%'||:q||'%'
      OR  p.prod_id::text = :q
    )
    AND (:ids_is_null OR p.prod_id = ANY(:ids))
)
SELECT
  fp.prod_id,
  fp.prod_name,
  pc.category_name,
  ss.weight_sold AS weight,
  ss.sale_date   AS dt
FROM stock_sales ss
JOIN filtered_product fp ON fp.prod_id = ss.prod_id
LEFT JOIN product_categories pc ON pc.category_id = fp.category_id
WHERE (:date_from IS NULL OR ss.sale_date >= :date_from)
  AND (:date_to_ex IS NULL OR ss.sale_date < :date_to_ex)
ORDER BY ss.sale_date DESC, ss.stock_sales_id DESC
""")

    rows = db.execute(sql, {
        "only_active": only_active,
        "category_id": category_id,
        "q": q,
        "ids_is_null": ids_list is None,
        "ids": ids_list or [],
        "date_from": date_from,
        "date_to_ex": date_to_exclusive,
    }).mappings().all()

    # สร้าง Excel
    wb = Workbook()
    ws = wb.active
    ws.title = "ขายออกทั้งหมด"

    headers = ["รหัส", "ชื่อสินค้า", "หมวดหมู่", "น้ำหนักที่ขาย (kg)", "วันที่ขาย"]
    ws.append(headers)

    for r in rows:
        prod_code = f'#{str(r["prod_id"]).zfill(3)}'
        dt = r["dt"].strftime("%Y-%m-%d %H:%M") if r["dt"] else ""
        ws.append([
            prod_code,
            r["prod_name"] or "",
            r["category_name"] or "",
            float(r["weight"]) if r["weight"] is not None else 0.0,
            dt
        ])

    # ปรับความกว้างคอลัมน์
    for col in ws.columns:
        max_len = 0
        col_letter = col[0].column_letter
        for cell in col:
            max_len = max(max_len, len(str(cell.value)) if cell.value is not None else 0)
        ws.column_dimensions[col_letter].width = max_len + 2

    output = BytesIO()
    wb.save(output)
    output.seek(0)

    filename = "รายงานขายออกทั้งหมด.xlsx"
    encoded_name = quote(filename)
    headers = {"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_name}"}

    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers=headers,
    )