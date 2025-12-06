from fastapi import APIRouter , Depends , Query
from sqlalchemy.orm import Session
from sqlalchemy import text
from pydantic import BaseModel
from app.database import SessionLocal
from typing import List, Optional

router = APIRouter(prefix="/purchase", tags=["Purchase_order"])
 

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

class PurchaseItem(BaseModel):
    purchase_item_id: int
    prod_name: str
    category_name: str
    purchase_date: str
    purchase_time: str
    weight: float
    price: float
    payment_method: Optional[str]

class PurchaseItemResponse(BaseModel):
    items: List[PurchaseItem]
    total_items: int
    total_pages: int
    current_page: int
    per_page: int

@router.get("/list",response_model=PurchaseItemResponse)
def list_purchase_items(
    q: str | None = Query(None),
    category_id: int | None = Query(None),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    page: int = Query (1 , ge=1),
    per_page: int = Query(20, ge=1 , le=200),
    db: Session = Depends(get_db)
):
    offset = (page - 1) * per_page

    count_sql = text("""
    SELECT COUNT(*)
    FROM purchase_items pi
    JOIN product pr            ON pr.prod_id = pi.prod_id
    JOIN product_categories cat ON cat.category_id = pr.category_id
    LEFT JOIN purchases pu     ON pu.purchase_id = pi.purchase_id
    LEFT JOIN payment pay      ON pay.purchase_id = pi.purchase_id
    WHERE (:q IS NULL OR pr.prod_name ILIKE '%' || :q || '%')
      AND (:category_id IS NULL OR pr.category_id = :category_id)
      AND (:date_from IS NULL OR pi.purchase_items_date >= :date_from::date)
      AND (:date_to   IS NULL OR pi.purchase_items_date <  (:date_to::date + INTERVAL '1 day'))
""")

    total_items = db.execute(
        count_sql,
        {"q": q, "category_id": category_id, "date_from": date_from, "date_to": date_to}
    ).scalar_one()

    sql = text("""
    SELECT
        pi.purchase_item_id,
        pr.prod_name,
        cat.category_name,
        TO_CHAR(pi.purchase_items_date,'DD/MM/YYYY') AS purchase_date,
        TO_CHAR(pi.purchase_items_date,'HH24:MI') AS purchase_time,
        pi.weight,
        pi.price,
        pay.payment_method
    FROM purchase_items pi
    JOIN product pr            ON pr.prod_id = pi.prod_id
    JOIN product_categories cat ON cat.category_id = pr.category_id
    LEFT JOIN purchases pu     ON pu.purchase_id = pi.purchase_id
    LEFT JOIN payment pay      ON pay.purchase_id = pi.purchase_id
    WHERE (:q IS NULL OR pr.prod_name ILIKE '%' || :q || '%')
      AND (:category_id IS NULL OR pr.category_id = :category_id)
      AND (:date_from IS NULL OR pi.purchase_items_date >= :date_from::date)
      AND (:date_to   IS NULL OR pi.purchase_items_date <  (:date_to::date + INTERVAL '1 day'))
    ORDER BY pi.purchase_items_date DESC
    LIMIT :per_page OFFSET :offset
""")

    rows = db.execute(sql,{"q": q,"category_id": category_id,"date_from": date_from,"date_to": date_to,"per_page": per_page,"offset": offset}).mappings().all()

    total_pages = (total_items + per_page - 1) // per_page

    return PurchaseItemResponse(
        items=rows,
        total_items=total_items,
        total_pages=total_pages,
        current_page=page,
        per_page=per_page
    )

@router.get("/customer_info_by_product/{prod_id}")
def customer_info_by_product(prod_id: int, db: Session = Depends(get_db)):
    sql = text("""
        SELECT
            c.customer_id,
            c.full_name,
            c.national_id,
            c.address,
            cp.photo_path
        FROM purchase_items pi
        JOIN purchases pu ON pu.purchase_id = pi.purchase_id
        JOIN customers c ON c.customer_id = pu.customer_id
        LEFT JOIN customer_photos cp ON cp.customer_id = c.customer_id
        WHERE pi.prod_id = :prod_id
        ORDER BY pi.purchase_items_date DESC
        LIMIT 1;
    """)

    row = db.execute(sql, {"prod_id": prod_id}).mappings().first()

    return row or {}
