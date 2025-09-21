from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import text
from app.database import SessionLocal

router = APIRouter(prefix="/receipts", tags=["receipts"])

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@router.get("/{purchase_id}")
def get_receipt(purchase_id: int, db: Session = Depends(get_db)):
    # หัวบิล + ลูกค้า
    head = db.execute(
        text("""
            SELECT p.purchase_id, p.customer_id, p.purchase_date, p.purchase_status,
                   c.full_name, c.national_id, c.address
            FROM purchases p
            LEFT JOIN customers c ON c.customer_id = p.customer_id
            WHERE p.purchase_id = :pid
        """),
        {"pid": purchase_id},
    ).mappings().first()
    if not head:
        raise HTTPException(status_code=404, detail="Purchase not found")

    # รายการ + รูปต่อรายการ
    items = db.execute(
        text("""
            SELECT i.purchase_item_id, i.prod_id, pr.prod_name, i.weight, i.price,
                   (i.weight * i.price) AS line_total
            FROM purchase_items i
            LEFT JOIN product pr ON pr.prod_id = i.prod_id
            WHERE i.purchase_id = :pid
            ORDER BY i.purchase_item_id
        """),
        {"pid": purchase_id},
    ).mappings().all()

    photos = db.execute(
        text("""
            SELECT ph.photo_id, ph.purchase_item_id, ph.img_path
            FROM purchase_item_photos ph
            JOIN purchase_items i ON i.purchase_item_id = ph.purchase_item_id
            WHERE i.purchase_id = :pid
            ORDER BY ph.photo_id
        """),
        {"pid": purchase_id},
    ).mappings().all()

    # ชำระเงิน + สลิป
    payment = db.execute(
        text("""
            SELECT payment_id, purchase_id, payment_method, payment_amount, payment_date
            FROM payment
            WHERE purchase_id = :pid
            ORDER BY payment_id DESC
            LIMIT 1
        """),
        {"pid": purchase_id},
    ).mappings().first()

    pay_photos = []
    if payment:
        pay_photos = db.execute(
            text("""
                SELECT photo_id, payment_id, payment_img
                FROM payment_photo
                WHERE payment_id = :payid
                ORDER BY photo_id
            """),
            {"payid": payment["payment_id"]},
        ).mappings().all()

    # รวมยอด
    total = db.execute(
        text("SELECT COALESCE(SUM(weight*price),0) AS total_amount FROM purchase_items WHERE purchase_id=:pid"),
        {"pid": purchase_id},
    ).mappings().first()

    return {
        "header": dict(head),
        "items": [dict(i) for i in items],
        "item_photos": [dict(p) for p in photos],
        "payment": dict(payment) if payment else None,
        "payment_photos": [dict(p) for p in pay_photos],
        "summary": {"total_amount": total["total_amount"]},
    }
