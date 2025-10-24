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
import requests
from pathlib import Path
import base64, uuid, os, tempfile

router = APIRouter(prefix="/purchases", tags=["payments"])

# ---------- ENV ----------
HARDWARE_BASE_URL = os.getenv("HARDWARE_BASE_URL", "http://localhost:9000")
STORE_NAME = os.getenv("STORE_NAME", "SCRAP SHOP")

UPLOAD_ROOT = os.getenv("UPLOAD_ROOT", "/app/uploads")  

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

def _save_receipt_from_base64(b64: str) -> str:
    try:
        img_bytes = base64.b64decode(b64, validate=True)
    except Exception:
        raise HTTPException(status_code=400, detail="receipt_base64 ไม่ถูกต้อง")

    target_dir = Path(UPLOAD_ROOT) / "receipts"
    target_dir.mkdir(parents=True, exist_ok=True)

    fname = f"{uuid.uuid4().hex}.jpg"         # บังคับ .jpg ให้เรียบง่าย
    abs_path = target_dir / fname
    rel_path = f"/uploads/receipts/{fname}"   # path ที่ frontend ใช้

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=target_dir, delete=False) as tmp:
            tmp.write(img_bytes)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp_path = Path(tmp.name)
        os.replace(tmp_path, abs_path)        # atomic move
    finally:
        if tmp_path and tmp_path.exists():
            try: tmp_path.unlink()
            except: pass

    return rel_path

def _save_receipt_bytes(img_bytes: bytes) -> str:
    target_dir = Path(UPLOAD_ROOT) / "receipts"
    target_dir.mkdir(parents=True, exist_ok=True)

    fname = f"{uuid.uuid4().hex}.jpg"        # บังคับ .jpg
    abs_path = target_dir / fname
    rel_path = f"/uploads/receipts/{fname}"

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=target_dir, delete=False) as tmp:
            tmp.write(img_bytes)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp_path = Path(tmp.name)
        os.replace(tmp_path, abs_path)       # atomic move
    finally:
        if tmp_path and tmp_path.exists():
            try:
                tmp_path.unlink()
            except:
                pass

    return rel_path

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
    receipt_img: Optional[str] = None
    

# ---------- 1) ชำระเงินบิล ----------
@router.post("/{purchase_id}/pay", response_model=PayResponse)
def pay_purchase(
    purchase_id: int,
    body: PayRequest,
    db: Session = Depends(get_db),
):
    _ensure_open(db, purchase_id)

    # กันจ่ายซ้ำ
    if _has_payment(db, purchase_id):
        raise HTTPException(status_code=409, detail="This purchase already has a payment")

    # รวมยอด
    total_w, total_a = _calc_summary(db, purchase_id)
    if total_a <= 0:
        raise HTTPException(status_code=400, detail="Total amount must be greater than 0")
    amount = _round_money(Decimal(str(total_a)))

    receipt_img_path: Optional[str] = None

    try:
        # 1) บันทึกการชำระ
        payment = db.execute(text("""
            INSERT INTO payment (purchase_id, payment_method, payment_amount)
            VALUES (:pid, :m, :amt)
            RETURNING payment_id, payment_date
        """), {"pid": purchase_id, "m": body.payment_method, "amt": amount}).mappings().first()

        pay_id = payment["payment_id"]

        # 2) เรนเดอร์รูปบิล "ทุกครั้ง" (ไม่ต้องรอ frontend ส่งรูป)
        rc_no = f"RC-{pay_id:06d}"
        payload = _build_receipt_payload(db, purchase_id, amount, rc_no)
        try:
            r = requests.post(
                f"{HARDWARE_BASE_URL.rstrip('/')}/printer/render",
                json=payload,
                timeout=10
            )
            r.raise_for_status()
            jpeg_bytes = r.content

            # เซฟไฟล์ + ลงตาราง payment_photo (คงเหลือ 1 รูป/บิล)
            receipt_img_path = _save_receipt_bytes(jpeg_bytes)
            db.execute(text("DELETE FROM payment_photo WHERE payment_id = :pid"), {"pid": pay_id})
            db.execute(text("""
                INSERT INTO payment_photo (payment_id, payment_img)
                VALUES (:pid, :img)
            """), {"pid": pay_id, "img": receipt_img_path})

        except Exception as e:
            # ถ้าอยาก “บังคับให้มีรูปเสมอ” ให้เปลี่ยนเป็น raise HTTPException(502, ...)
            print(f"[WARN] render receipt image failed: {e}")

        # 3) ปิดบิล
        db.execute(text("""
            UPDATE purchases
               SET purchase_status = 'DONE'
             WHERE purchase_id = :pid
        """), {"pid": purchase_id})

        db.commit()

    except IntegrityError as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=f"Database constraint failed: {str(e.orig)}")
    except Exception:
        db.rollback()
        raise

    # 4) (ออปชัน) สั่งพิมพ์จริง
    printed_ok: Optional[bool] = None
    print_err: Optional[str] = None
    if body.print_receipt:
        rc_no = f"RC-{payment['payment_id']:06d}"
        payload = _build_receipt_payload(db, purchase_id, amount, rc_no)
        printed_ok, print_err = _try_print_receipt(payload)

    # ดึง path ล่าสุด (กันพลาด)
    photo = db.execute(text("""
        SELECT payment_img FROM payment_photo
        WHERE payment_id = :pid
        LIMIT 1
    """), {"pid": payment["payment_id"]}).mappings().first()
    receipt_img_path = photo["payment_img"] if photo else receipt_img_path

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
        receipt_img=receipt_img_path
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
