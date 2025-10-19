# backend/app/routers/payments.py
from decimal import Decimal, ROUND_HALF_UP
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from typing import Optional, Literal, List, Tuple
from sqlalchemy.orm import Session
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from app.database import SessionLocal
from datetime import datetime
import os
import requests

router = APIRouter(prefix="/purchases", tags=["payments"])

# ---------- ENV ----------
HARDWARE_BASE_URL = os.getenv("HARDWARE_BASE_URL", "http://localhost:9000")
STORE_NAME = os.getenv("STORE_NAME", "SCRAP SHOP")

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

def _calc_summary(db: Session, purchase_id: int) -> Tuple[Decimal, Decimal]:
    # NOTE: จากสคีมาของคุณ: purchase_items.weight, purchase_items.price
    # ในระบบจริง price มักเป็น 'amount (ราคารวมต่อแถว)' จึง SUM(price) = total_amount
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

def _get_customer_name(db: Session, purchase_id: int) -> str:
    row = db.execute(text("""
        SELECT c.full_name AS cname
        FROM purchases p
        LEFT JOIN customers c ON p.customer_id = c.customer_id
        WHERE p.purchase_id = :pid
        LIMIT 1
    """), {"pid": purchase_id}).mappings().first()
    name = (row["cname"] if row else None) or "ไม่ระบุ"
    return name

def _get_items_for_receipt(db: Session, purchase_id: int) -> List[dict]:
    rows = db.execute(text("""
        SELECT
          COALESCE(pr.prod_name, '-') AS name,
          pi.weight AS weight,
          pi.price  AS line_amount
        FROM purchase_items pi
        LEFT JOIN product pr ON pi.prod_id = pr.prod_id
        WHERE pi.purchase_id = :pid
        ORDER BY pi.purchase_item_id ASC
    """), {"pid": purchase_id}).mappings().all()

    items = []
    for r in rows:
        w = Decimal(str(r["weight"] or 0))
        amt = Decimal(str(r["line_amount"] or 0))
        unit_price = Decimal("0.00")
        if w and w > 0:
            unit_price = (amt / w).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        items.append({
            "name": str(r["name"] or "-"),
            "qty": float(w),
            "unit": "kg",
            "price": float(unit_price)
        })
    return items

def _build_receipt_payload(db: Session, purchase_id: int, total_amount: Decimal, receipt_no: str) -> dict:
    customer_name = _get_customer_name(db, purchase_id)
    items = _get_items_for_receipt(db, purchase_id)
    return {
        "store_name": STORE_NAME,
        "receipt_no": receipt_no,
        "customer_name": customer_name,
        "items": items,
        "total": float(_round_money(Decimal(str(total_amount))))
    }

def _try_print_receipt(payload: dict) -> Tuple[bool, Optional[str]]:
    """
    เรียก Hardware Service เพื่อพิมพ์ใบเสร็จ
    คืนค่า (printed_ok, error_message)
    """
    url = f"{HARDWARE_BASE_URL.rstrip('/')}/printer/receipt"
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            return True, None
        else:
            return False, f"HTTP {resp.status_code}: {resp.text}"
    except Exception as e:
        return False, str(e)

# ---------- Schemas ----------
class PayRequest(BaseModel):
    payment_method: Literal["เงินสด", "เงินโอน"] = Field(description="วิธีชำระ")
    print_receipt: bool = Field(default=False, description="ถ้าจริงให้สั่งพิมพ์")

class PayResponse(BaseModel):
    purchase_id: int
    payment_id: int
    payment_method: str
    payment_amount: Decimal
    paid_at: datetime
    will_print: bool
    printed: Optional[bool] = None      
    print_error: Optional[str] = None   
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

    amount = _round_money(Decimal(str(total_a)))

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
        raise HTTPException(status_code=400, detail=f"Database constraint failed: {str(e.orig)}")
    except Exception as e:
        db.rollback()
        raise

    # ---------- พิมพ์ใบเสร็จ (เลือกได้) ----------
    printed_ok: Optional[bool] = None
    print_err: Optional[str] = None

    if body.print_receipt:
        rc_no = f"RC-{payment['payment_id']:06d}"
        payload = _build_receipt_payload(db, purchase_id, amount, rc_no)
        printed_ok, print_err = _try_print_receipt(payload)


    return PayResponse(
        purchase_id=purchase_id,
        payment_id=payment["payment_id"],
        payment_method=body.payment_method,
        payment_amount=amount,
        paid_at=payment["payment_date"],
        will_print=body.print_receipt,
        printed=printed_ok,
        print_error=print_err,
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
