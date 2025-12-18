from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from sqlalchemy import text
from pydantic import BaseModel
from typing import List, Optional
from datetime import date, datetime, timedelta

from app.database import SessionLocal

router = APIRouter(
    prefix="/receipts",
    tags=["Receipts"]
)

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
    date: str
    time: str
    amount: Optional[float]
    payment_type: Optional[str]


class ReceiptListResponse(BaseModel):
    items: List[ReceiptRow]
    total_items: int
    total_pages: int
    current_page: int
    per_page: int


class ReceiptImage(BaseModel):
    photo_id: int
    image: str


class ReceiptImageResponse(BaseModel):
    purchase_id: int
    images: List[ReceiptImage]


# ---------- helper ----------
def _make_date_from_dt(d: date | None):
    if d is None:
        return None
    return datetime.combine(d, datetime.min.time())


def _make_date_to_exclusive(d: date | None):
    if d is None:
        return None
    return datetime.combine(d, datetime.min.time()) + timedelta(days=1)


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
    date_from_dt = _make_date_from_dt(date_from)
    date_to_excl = _make_date_to_exclusive(date_to)

    params = {
        "date_from": date_from_dt,
        "date_to_excl": date_to_excl,
        "limit": per_page,
        "offset": offset,
    }

    # ---------- COUNT ----------
    count_sql = text("""
        SELECT COUNT(*)
        FROM purchases pu
        WHERE pu.purchase_status = 'DONE'
          AND (:date_from IS NULL OR pu.purchase_date >= :date_from)
          AND (:date_to_excl IS NULL OR pu.purchase_date < :date_to_excl)
    """)

    total_items = db.execute(count_sql, params).scalar() or 0

    # ---------- DATA ----------
    sql = text("""
        SELECT
            pu.purchase_id,
            TO_CHAR(pu.purchase_date, 'YYYY-MM-DD') AS date,
            TO_CHAR(pu.purchase_date, 'HH24:MI')    AS time,
            pay.payment_amount                      AS amount,
            pay.payment_method                      AS payment_type
        FROM purchases pu
        LEFT JOIN payment pay
            ON pay.purchase_id = pu.purchase_id
        WHERE pu.purchase_status = 'DONE'
          AND (:date_from IS NULL OR pu.purchase_date >= :date_from)
          AND (:date_to_excl IS NULL OR pu.purchase_date < :date_to_excl)
        ORDER BY pu.purchase_date DESC
        LIMIT :limit OFFSET :offset
    """)

    items = db.execute(sql, params).mappings().all()

    total_pages = (total_items + per_page - 1) // per_page

    return {
        "items": items,
        "total_items": total_items,
        "total_pages": total_pages,
        "current_page": page,
        "per_page": per_page,
    }


# ---------- Receipt Images ----------
@router.get("/{purchase_id}/images", response_model=ReceiptImageResponse)
def receipt_images(
    purchase_id: int,
    db: Session = Depends(get_db),
):
    sql = text("""
        SELECT
            pp.photo_id,
            pp.payment_img
        FROM payment pay
        JOIN payment_photo pp
            ON pp.payment_id = pay.payment_id
        WHERE pay.purchase_id = :purchase_id
        ORDER BY pp.photo_id
    """)

    rows = db.execute(
        sql,
        {"purchase_id": purchase_id},
    ).mappings().all()

    BASE_URL = "http://localhost:8080"

    images = [
        {
            "photo_id": r["photo_id"],
            "image": f"{BASE_URL.rstrip('/')}/{r['payment_img'].lstrip('/')}",
        }
        for r in rows
    ]

    return {
        "purchase_id": purchase_id,
        "images": images,
    }
