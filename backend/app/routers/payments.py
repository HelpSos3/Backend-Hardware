# backend/app/routers/payments.py
from decimal import Decimal
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from sqlalchemy import text
from app.database import SessionLocal
import os, uuid, shutil, datetime

router = APIRouter(prefix="/payments", tags=["payments"])

# -------- Config uploads --------
UPLOAD_ROOT = os.getenv("UPLOAD_ROOT", "/app/app/uploads")
PAY_DIR = os.path.join(UPLOAD_ROOT, "payments")
os.makedirs(PAY_DIR, exist_ok=True)
ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".webp"}

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# -------- Schemas (response) --------
class PaymentPhotoOut(BaseModel):
    photo_id: int
    payment_img: str

class PaymentOut(BaseModel):
    payment_id: int
    purchase_id: int
    payment_method: str
    payment_amount: Decimal
    payment_date: datetime.datetime
    photos: List[PaymentPhotoOut] = []

# -------- Helpers --------
def _ensure_purchase_open(db: Session, purchase_id: int):
    row = db.execute(
        text("SELECT purchase_status FROM purchases WHERE purchase_id=:pid"),
        {"pid": purchase_id}
    ).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Purchase not found")
    if row["purchase_status"] != "OPEN":
        raise HTTPException(status_code=409, detail="Purchase is not OPEN")

def _sum_purchase(db: Session, purchase_id: int):
    row = db.execute(
        text("""
            SELECT
              COALESCE(SUM(weight), 0) AS total_weight,
              COALESCE(SUM(price), 0)  AS total_amount
            FROM purchase_items
            WHERE purchase_id=:pid
        """),
        {"pid": purchase_id}
    ).mappings().first()
    return row or {"total_weight": Decimal("0"), "total_amount": Decimal("0")}

def _save_payment_photos(db: Session, payment_id: int, files: List[UploadFile]) -> List[PaymentPhotoOut]:
    out: List[PaymentPhotoOut] = []
    if not files:
        return out
    for f in files:
        if not f or not f.filename:
            continue
        _, ext = os.path.splitext(f.filename)
        ext = (ext or "").lower()
        if ext not in ALLOWED_EXT:
            raise HTTPException(status_code=400, detail="รองรับ .jpg .jpeg .png .webp เท่านั้น")

        fname = f"{uuid.uuid4().hex}{ext}"
        rel = f"payments/{fname}"
        abs_path = os.path.join(UPLOAD_ROOT, rel)
        with open(abs_path, "wb") as w:
            shutil.copyfileobj(f.file, w)

        row = db.execute(
            text("""INSERT INTO payment_photo (payment_id, payment_img)
                    VALUES (:pid, :p)
                    RETURNING photo_id, payment_img"""),
            {"pid": payment_id, "p": f"/uploads/{rel}"}
        ).mappings().first()
        out.append(PaymentPhotoOut(photo_id=row["photo_id"], payment_img=row["payment_img"]))
    return out

# -------- Create & pay (with photos) --------
@router.post("/pay", response_model=PaymentOut, status_code=201)
async def create_payment_and_close(
    purchase_id: int = Form(..., description="รหัสบิลที่จะปิด"),
    payment_method: str = Form(..., description="เงินสด | เงินโอน"),
    payment_amount: Decimal = Form(..., ge=0),
    # แนบไฟล์: เงินโอน → สลิป + บิล / เงินสด → บิลอย่างเดียว
    slipFiles: Optional[List[UploadFile]] = File(default=None, description="รูปสลิป (สำหรับเงินโอน)"),
    billFiles: Optional[List[UploadFile]] = File(default=None, description="รูปบิล/ใบเสร็จ"),
    db: Session = Depends(get_db),
):
    if payment_method not in ("เงินสด", "เงินโอน"):
        raise HTTPException(status_code=400, detail="payment_method ต้องเป็น 'เงินสด' หรือ 'เงินโอน'")

    _ensure_purchase_open(db, purchase_id)

    # ตรวจยอด (ถ้าต้องการ strict ให้เปิดบรรทัดเทียบเท่า)
    summary = _sum_purchase(db, purchase_id)
    expected = summary["total_amount"]
    # if payment_amount != expected:
    #     raise HTTPException(status_code=400, detail=f"ยอดชำระ {payment_amount} ไม่ตรงกับยอดบิล {expected}")

    # เงื่อนไขไฟล์ตามวิธีชำระ
    slip_list = slipFiles or []
    bill_list = billFiles or []

    if payment_method == "เงินโอน":
        if len(slip_list) == 0:
            raise HTTPException(status_code=400, detail="ต้องแนบรูปสลิปอย่างน้อย 1 รูปสำหรับเงินโอน")
        if len(bill_list) == 0:
            raise HTTPException(status_code=400, detail="ต้องแนบรูปบิล/ใบเสร็จอย่างน้อย 1 รูป")
    else:  # เงินสด
        if len(bill_list) == 0:
            raise HTTPException(status_code=400, detail="ต้องแนบรูปบิล/ใบเสร็จอย่างน้อย 1 รูป")

    # สร้าง payment
    pay = db.execute(
        text("""INSERT INTO payment (purchase_id, payment_method, payment_amount)
                VALUES (:pid, :m, :amt)
                RETURNING payment_id, purchase_id, payment_method, payment_amount, payment_date"""),
        {"pid": purchase_id, "m": payment_method, "amt": payment_amount}
    ).mappings().first()

    # บันทึกรูปทั้งหมด (สลิป + บิล)
    photos: List[PaymentPhotoOut] = []
    photos += _save_payment_photos(db, pay["payment_id"], slip_list)
    photos += _save_payment_photos(db, pay["payment_id"], bill_list)

    # ปิดบิล
    db.execute(
        text("""UPDATE purchases
                   SET purchase_status='PAID',
                       updated_at = NOW()
                 WHERE purchase_id=:pid AND purchase_status='OPEN'"""),
        {"pid": purchase_id}
    )

    db.commit()

    return PaymentOut(
        payment_id=pay["payment_id"],
        purchase_id=pay["purchase_id"],
        payment_method=pay["payment_method"],
        payment_amount=pay["payment_amount"],
        payment_date=pay["payment_date"],
        photos=photos,
    )

# -------- อ่านข้อมูลการชำระเงินของบิล --------
@router.get("/by-purchase/{purchase_id}", response_model=List[PaymentOut])
def list_payments_of_purchase(purchase_id: int, db: Session = Depends(get_db)):
    pays = db.execute(
        text("""SELECT payment_id, purchase_id, payment_method, payment_amount, payment_date
                FROM payment
                WHERE purchase_id=:pid
                ORDER BY payment_id DESC"""),
        {"pid": purchase_id}
    ).mappings().all()

    out: List[PaymentOut] = []
    for p in pays:
        ph = db.execute(
            text("""SELECT photo_id, payment_img
                    FROM payment_photo
                    WHERE payment_id=:pid
                    ORDER BY photo_id ASC"""),
            {"pid": p["payment_id"]}
        ).mappings().all()
        out.append(PaymentOut(
            **dict(p),
            photos=[PaymentPhotoOut(photo_id=r["photo_id"], payment_img=r["payment_img"]) for r in ph]
        ))
    return out

# -------- แนบรูปเพิ่มภายหลัง (ถ้าจำเป็น) --------
@router.post("/{payment_id}/photos", response_model=List[PaymentPhotoOut], status_code=201)
async def add_payment_photos(
    payment_id: int,
    files: List[UploadFile] = File(...),
    db: Session = Depends(get_db),
):
    ok = db.execute(text("SELECT 1 FROM payment WHERE payment_id=:pid"), {"pid": payment_id}).first()
    if not ok:
        raise HTTPException(status_code=404, detail="Payment not found")

    photos = _save_payment_photos(db, payment_id, files)
    db.commit()
    return photos

# -------- ลบรูป --------
@router.delete("/{payment_id}/photos/{photo_id}", status_code=204)
def delete_payment_photo(payment_id: int, photo_id: int, db: Session = Depends(get_db)):
    res = db.execute(
        text("""DELETE FROM payment_photo WHERE photo_id=:phid AND payment_id=:pid"""),
        {"phid": photo_id, "pid": payment_id}
    )
    db.commit()
    if res.rowcount == 0:
        raise HTTPException(status_code=404, detail="Photo not found")
