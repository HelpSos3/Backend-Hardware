# backend/app/routers/payments.py
from decimal import Decimal, ROUND_HALF_UP
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from typing import Optional, Literal
from sqlalchemy.orm import Session
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from app.database import SessionLocal
from datetime import datetime

router = APIRouter(prefix="/purchases", tags=["payments"])

# ---------- DB Session ----------
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ---------- Helpers ----------
def _round_money(value: Decimal) -> Decimal:
    # ปัด 2 ตำแหน่ง แบบ half up
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

def _ensure_open(db: Session, purchase_id: int):
    st = db.execute(
        text("SELECT purchase_status FROM purchases WHERE purchase_id=:pid"),
        {"pid": purchase_id}
    ).mappings().first()
    if not st:
        raise HTTPException(status_code=404, detail="Purchase not found")
    if st["purchase_status"] != "OPEN":
        raise HTTPException(status_code=409, detail="Purchase is not OPEN")

def _calc_summary(db: Session, purchase_id: int):
    row = db.execute(text("""
        SELECT
          COALESCE(SUM(weight), 0) AS total_weight,
          COALESCE(SUM(price), 0)  AS total_amount
        FROM purchase_items
        WHERE purchase_id = :pid
    """), {"pid": purchase_id}).mappings().first()
    total_weight = Decimal(str(row["total_weight"]))
    total_amount = Decimal(str(row["total_amount"]))
    return total_weight, _round_money(total_amount)

def _has_payment(db: Session, purchase_id: int) -> bool:
    r = db.execute(
        text("SELECT 1 FROM payment WHERE purchase_id=:pid LIMIT 1"),
        {"pid": purchase_id}
    ).first()
    return bool(r)

# ---------- Schemas ----------
class PayRequest(BaseModel):
    payment_method: Literal["เงินสด", "เงินโอน"] = Field(description="วิธีชำระ")
    print_receipt: bool = Field(default=False, description="ถ้าจริงให้สั่งพิมพ์ (ตอนนี้ยังไม่พิมพ์)")

class PayResponse(BaseModel):
    purchase_id: int
    payment_id: int
    payment_method: str
    payment_amount: Decimal
    paid_at: datetime
    will_print: bool
    purchase_status: str
    summary_weight: Decimal
    summary_amount: Decimal

# ---------- 1) ชำระเงินบิล ----------
@router.post("/{purchase_id}/pay", response_model=PayResponse)
def pay_purchase(
    purchase_id: int,
    body: PayRequest,
    db: Session = Depends(get_db),
):
    _ensure_open(db, purchase_id)

    # ป้องกันชำระซ้ำ
    if _has_payment(db, purchase_id):
        raise HTTPException(status_code=409, detail="This purchase already has a payment")

    # คำนวณยอดรวมจากรายการ
    total_w, total_a = _calc_summary(db, purchase_id)
    if total_a <= 0:
        raise HTTPException(status_code=400, detail="Total amount must be greater than 0")

    amount = total_a
    amount = _round_money(Decimal(str(amount)))

    try:
        # 1) บันทึกการชำระ
        payment = db.execute(text("""
            INSERT INTO payment (purchase_id, payment_method, payment_amount)
            VALUES (:pid, :m, :amt)
            RETURNING payment_id, payment_date
        """), {"pid": purchase_id, "m": body.payment_method, "amt": amount}).mappings().first()

        # 2) ปิดบิล
        db.execute(text("""
            UPDATE purchases
               SET purchase_status = 'DONE'
             WHERE purchase_id = :pid
        """), {"pid": purchase_id})

        # ยืนยันธุรกรรม
        db.commit()

    except IntegrityError as e:
        db.rollback()
        # เช่น payment_method ผิดจาก CHECK, FK ผิด ฯลฯ
        raise HTTPException(status_code=400, detail=f"Database constraint failed: {str(e.orig)}")
    except Exception as e:
        db.rollback()
        raise

    # (ตอนนี้ยังไม่พิมพ์จริง) — ส่ง will_print เป็นค่าที่ผู้ใช้เลือกไว้
    return PayResponse(
        purchase_id=purchase_id,
        payment_id=payment["payment_id"],
        payment_method=body.payment_method,
        payment_amount=amount,
        paid_at=payment["payment_date"],
        will_print=body.print_receipt,
        purchase_status="DONE",
        summary_weight=total_w,
        summary_amount=total_a,
    )

# ---------- 2) ดูสถานะการชำระ (สำหรับหน้า Receipt) ----------
class PaymentInfo(BaseModel):
    purchase_id: int
    status: str
    total_weight: Decimal
    total_amount: Decimal
    payment_id: Optional[int] = None
    payment_method: Optional[str] = None
    payment_amount: Optional[Decimal] = None
    paid_at: Optional[datetime] = None

@router.get("/{purchase_id}/payment", response_model=PaymentInfo)
def get_payment_info(purchase_id: int, db: Session = Depends(get_db)):
    # ข้อมูลบิล
    pr = db.execute(text("""
        SELECT purchase_status FROM purchases WHERE purchase_id=:pid
    """), {"pid": purchase_id}).mappings().first()
    if not pr:
        raise HTTPException(status_code=404, detail="Purchase not found")

    total_w, total_a = _calc_summary(db, purchase_id)

    pay = db.execute(text("""
        SELECT payment_id, payment_method, payment_amount, payment_date
        FROM payment WHERE purchase_id=:pid
        ORDER BY payment_id DESC
        LIMIT 1
    """), {"pid": purchase_id}).mappings().first()

    return PaymentInfo(
        purchase_id=purchase_id,
        status=pr["purchase_status"],
        total_weight=total_w,
        total_amount=total_a,
        payment_id=pay["payment_id"] if pay else None,
        payment_method=pay["payment_method"] if pay else None,
        payment_amount=pay["payment_amount"] if pay else None,
        paid_at=pay["payment_date"] if pay else None,
    )
