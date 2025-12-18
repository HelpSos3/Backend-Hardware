from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from sqlalchemy import text
from pydantic import BaseModel
from typing import List, Optional
from datetime import date, timedelta

from app.database import SessionLocal

router = APIRouter(prefix="/receipts", tags=["Receipts"])


# ---------- DB Session ----------
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---------- Pydantic Models ----------
class ReceiptRow(BaseModel):
    purchase_id: int
    receipt_date: str
    receipt_time: str
    amount: Optional[float]
    payment_method: Optional[str]


class ReceiptListResponse(BaseModel):
    items: List[ReceiptRow]
    total_items: int
    total_pages: int
    current_page: int
    per_page: int


# ---------- Receipts List ----------
@router.get("", response_model=ReceiptListResponse)
def list_receipts(
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
):
    offset = (page - 1) * per_page

    
    date_to_exclusive: date | None = None
    if date_to is not None:
        date_to_exclusive = date_to + timedelta(days=1)

    params = {
        "date_from": date_from,
        "date_to_exclusive": date_to_exclusive,
        "limit": per_page,
        "offset": offset,
    }

    # ---------- COUNT ----------
    count_sql = text("""
        SELECT COUNT(*)
        FROM purchases pu
        LEFT JOIN payment pay ON pay.purchase_id = pu.purchase_id
        WHERE pu.purchase_status = 'DONE'
          AND (:date_from IS NULL OR pu.purchase_date >= :date_from)
          AND (:date_to_exclusive IS NULL OR pu.purchase_date < :date_to_exclusive)
    """)

    total_items = db.execute(count_sql, params).scalar_one()

    # ---------- DATA ----------
    sql = text("""
        SELECT
            pu.purchase_id,
            TO_CHAR(pu.purchase_date, 'DD/MM/YYYY') AS receipt_date,
            TO_CHAR(pu.purchase_date, 'HH24:MI')    AS receipt_time,
            pay.payment_amount                      AS amount,
            pay.payment_method
        FROM purchases pu
        LEFT JOIN payment pay ON pay.purchase_id = pu.purchase_id
        WHERE pu.purchase_status = 'DONE'
          AND (:date_from IS NULL OR pu.purchase_date >= :date_from)
          AND (:date_to_exclusive IS NULL OR pu.purchase_date < :date_to_exclusive)
        ORDER BY pu.purchase_date DESC
        LIMIT :limit OFFSET :offset
    """)

    rows = db.execute(sql, params).mappings().all()

    total_pages = (total_items + per_page - 1) // per_page

    return ReceiptListResponse(
        items=rows,
        total_items=total_items,
        total_pages=total_pages,
        current_page=page,
        per_page=per_page,
    )
