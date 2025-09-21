from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from typing import Optional
from sqlalchemy.orm import Session
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from datetime import datetime
import httpx
from app.database import SessionLocal
import os, uuid, requests, base64

router = APIRouter(prefix="/purchases", tags=["purchases"])

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ====== config ======
UPLOAD_ROOT = os.getenv("UPLOAD_ROOT", "/app/app/uploads")
HARDWARE_URL = os.getenv("HARDWARE_URL", "http://host.docker.internal:9000")
os.makedirs(os.path.join(UPLOAD_ROOT, "customer_photos"), exist_ok=True)
os.makedirs(os.path.join(UPLOAD_ROOT, "idcard_photos"), exist_ok=True)

# -------- Schemas --------
class PurchaseCreate(BaseModel):
    customer_id: Optional[int] = None

class PurchaseOut(BaseModel):
    purchase_id: int
    customer_id: Optional[int]
    purchase_date: datetime
    purchase_status: str
    updated_at: datetime
    customer_name: Optional[str] = None
    customer_national_id: Optional[str] = None
    customer_address: Optional[str] = None
    photo_path: Optional[str] = None
    resumed: bool = False
    notice: Optional[str] = None  

class IdCardPreviewOut(BaseModel):
    full_name: Optional[str] = None
    national_id: Optional[str] = None
    address: Optional[str] = None
    photo_base64: Optional[str] = None

# -------- Helpers --------
def _get_open_with_customer(db: Session):
    return db.execute(
        text("""
        SELECT p.purchase_id, p.customer_id, p.purchase_date, p.purchase_status, p.updated_at,
               c.full_name   AS customer_name,
               c.national_id AS customer_national_id,
               c.address     AS customer_address
        FROM purchases p
        LEFT JOIN customers c ON c.customer_id = p.customer_id
        WHERE p.purchase_status = 'OPEN'
        ORDER BY p.updated_at DESC
        LIMIT 1
        """)
    ).mappings().first()

def _get_customer_by_id(db: Session, cid: int):
    return db.execute(text("""
        SELECT 
            full_name   AS customer_name,
            national_id AS customer_national_id,
            address     AS customer_address
        FROM customers 
        WHERE customer_id = :cid
    """), {"cid": cid}).mappings().first()

def _get_latest_customer_photo(db: Session, cid: int) -> Optional[str]:
    if not cid:
        return None
    r = db.execute(text("""
        SELECT photo_path
        FROM customer_photos
        WHERE customer_id = :cid
        ORDER BY photo_id DESC
        LIMIT 1
    """), {"cid": cid}).mappings().first()
    return r["photo_path"] if r else None

def _upsert_customer_by_idcard(db: Session, full_name: str, national_id: str, address: str | None):
    exist = db.execute(
        text("SELECT customer_id FROM customers WHERE national_id = :nid"),
        {"nid": national_id},
    ).mappings().first()
    if exist:
        cid = exist["customer_id"]
        db.execute(
            text("""
                UPDATE customers
                   SET full_name = COALESCE(NULLIF(:full_name, ''), full_name),
                       address   = COALESCE(NULLIF(:address, ''), address)
                 WHERE customer_id = :cid
            """),
            {"full_name": full_name, "address": address, "cid": cid}
        )
        return cid
    row = db.execute(
        text("""
            INSERT INTO customers (full_name, national_id, address)
            VALUES (:full_name, :national_id, :address)
            RETURNING customer_id
        """),
        {"full_name": full_name, "national_id": national_id, "address": address},
    ).mappings().first()
    return row["customer_id"]

def _save_idcard_photo_if_present(db: Session, customer_id: int, photo_b64: str | None) -> str | None:
    if not photo_b64:
        return None
    fname = f"{uuid.uuid4().hex}.jpg"
    rel_path = f"idcard_photos/{fname}"
    abs_path = os.path.join(UPLOAD_ROOT, rel_path)
    with open(abs_path, "wb") as f:
        f.write(base64.b64decode(photo_b64))
    db.execute(
        text("""
            INSERT INTO customer_photos (customer_id, photo_path)
            VALUES (:cid, :p)
        """),
        {"cid": customer_id, "p": f"/uploads/{rel_path}"}
    )
    return f"/uploads/{rel_path}"

# -------- API --------
@router.get("/open", response_model=Optional[PurchaseOut])
def get_open_purchase(db: Session = Depends(get_db)):
    row = _get_open_with_customer(db)
    if not row:
        return None
    out = dict(row)
    out["resumed"] = True
    out["notice"] = f"มีบิลค้างอยู่ (เลขที่ {row['purchase_id']}) ของลูกค้า: {row['customer_name'] or 'ไม่ระบุ'}"
    out["photo_path"] = _get_latest_customer_photo(db, row["customer_id"])
    return out

# ====== quick-open anonymous ======
@router.post("/quick-open/anonymous", response_model=PurchaseOut, status_code=201)
def quick_open_anonymous(
    device_index: int = Query(0),
    warmup: int = Query(8, ge=0, le=30),
    on_open: str = Query("return"),
    confirm_delete: bool = Query(False),
    db: Session = Depends(get_db),
):
    open_row = _get_open_with_customer(db)
    if open_row:
        if on_open == "return":
            cust = _get_customer_by_id(db, open_row["customer_id"]) if open_row["customer_id"] else None
            return PurchaseOut(
                purchase_id=open_row["purchase_id"],
                customer_id=open_row["customer_id"],
                purchase_date=open_row["purchase_date"],
                purchase_status=open_row["purchase_status"],
                updated_at=open_row["updated_at"],
                customer_name=open_row["customer_name"],
                customer_national_id=(cust["customer_national_id"] if cust else None),
                customer_address=(cust["customer_address"] if cust else None),
                photo_path=_get_latest_customer_photo(db, open_row["customer_id"]),
                resumed=True,
                notice=f"มีบิลค้างอยู่ (เลขที่ {open_row['purchase_id']})",
            )
        if on_open == "delete_then_new":
            if not confirm_delete:
                raise HTTPException(status_code=400, detail="ต้องส่ง confirm_delete=true")
            db.execute(text("DELETE FROM purchases WHERE purchase_id=:pid AND purchase_status='OPEN'"),
                       {"pid": open_row["purchase_id"]})
            db.commit()
        if on_open == "error":
            raise HTTPException(status_code=409, detail="มีบิลค้างอยู่")

    # 1) ถ่ายรูป
    try:
        r = requests.post(f"{HARDWARE_URL}/camera/capture", params={"device_index": device_index, "warmup": warmup}, timeout=15)
        r.raise_for_status()
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Hardware error: {e}")
    img_bytes = r.content
    fname = f"{uuid.uuid4().hex}.jpg"
    rel_path = f"customer_photos/{fname}"
    abs_path = os.path.join(UPLOAD_ROOT, rel_path)
    with open(abs_path, "wb") as f:
        f.write(img_bytes)

    # 2) ลูกค้า anonymous
    row_cust = db.execute(text("INSERT INTO customers (full_name,national_id,address) VALUES (NULL,NULL,NULL) RETURNING customer_id")).mappings().first()
    cid = row_cust["customer_id"]
    db.execute(text("INSERT INTO customer_photos (customer_id, photo_path) VALUES (:cid,:p)"),
               {"cid": cid, "p": f"/uploads/{rel_path}"})

    # 3) เปิดบิล
    try:
        row = db.execute(text("INSERT INTO purchases (customer_id) VALUES (:cid) RETURNING purchase_id,customer_id,purchase_date,purchase_status,updated_at"),
                         {"cid": cid}).mappings().first()
        db.commit()
    except IntegrityError:
        db.rollback()
        open_row = _get_open_with_customer(db)
        if open_row:
            cust = _get_customer_by_id(db, open_row["customer_id"]) if open_row["customer_id"] else None
            return PurchaseOut(
                purchase_id=open_row["purchase_id"],
                customer_id=open_row["customer_id"],
                purchase_date=open_row["purchase_date"],
                purchase_status=open_row["purchase_status"],
                updated_at=open_row["updated_at"],
                customer_name=open_row["customer_name"],
                customer_national_id=(cust["customer_national_id"] if cust else None),
                customer_address=(cust["customer_address"] if cust else None),
                photo_path=_get_latest_customer_photo(db, open_row["customer_id"]) or f"/uploads/{rel_path}",
                resumed=True,
                notice="พบใบ OPEN ค้าง",
            )
        raise HTTPException(status_code=409, detail="ไม่สามารถเปิดบิลใหม่ได้")

    return PurchaseOut(
        purchase_id=row["purchase_id"],
        customer_id=row["customer_id"],
        purchase_date=row["purchase_date"],
        purchase_status=row["purchase_status"],
        updated_at=row["updated_at"],
        photo_path=f"/uploads/{rel_path}",
        resumed=False,
        notice="เปิดบิลใหม่เรียบร้อย (anonymous)",
    )

# ====== quick-open idcard ======
@router.post("/quick-open/idcard", response_model=PurchaseOut, status_code=201)
def quick_open_with_idcard(
    reader_index: int = Query(0),
    with_photo: int = Query(1),
    on_open: str = Query("return"),
    confirm_delete: bool = Query(False),
    db: Session = Depends(get_db),
):
    open_row = _get_open_with_customer(db)
    if open_row:
        if on_open == "return":
            cust = _get_customer_by_id(db, open_row["customer_id"]) if open_row["customer_id"] else None
            return PurchaseOut(
                purchase_id=open_row["purchase_id"],
                customer_id=open_row["customer_id"],
                purchase_date=open_row["purchase_date"],
                purchase_status=open_row["purchase_status"],
                updated_at=open_row["updated_at"],
                customer_name=open_row["customer_name"],
                customer_national_id=(cust["customer_national_id"] if cust else None),
                customer_address=(cust["customer_address"] if cust else None),
                photo_path=_get_latest_customer_photo(db, open_row["customer_id"]),
                resumed=True,
                notice="มีบิลค้างอยู่",
            )
        if on_open == "delete_then_new":
            if not confirm_delete:
                raise HTTPException(status_code=400, detail="ต้อง confirm_delete=true")
            db.execute(text("DELETE FROM purchases WHERE purchase_id=:pid AND purchase_status='OPEN'"),
                       {"pid": open_row["purchase_id"]})
            db.commit()
        if on_open == "error":
            raise HTTPException(status_code=409, detail="มีบิลค้างอยู่")

    # scan idcard
    try:
        r = requests.get(f"{HARDWARE_URL}/idcard/scan", params={"reader_index": reader_index, "with_photo": with_photo}, timeout=30)
        r.raise_for_status()
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"hardware error: {e}")
    payload = r.json()
    national_id = (payload.get("national_id") or "").strip()
    full_name = (payload.get("full_name") or "").strip()
    address = payload.get("address") or None
    if not national_id:
        raise HTTPException(status_code=400, detail="ไม่พบ national_id")

    cid = _upsert_customer_by_idcard(db, full_name, national_id, address)
    photo_path = None
    if with_photo and payload.get("photo_base64"):
        photo_path = _save_idcard_photo_if_present(db, cid, payload["photo_base64"])

    try:
        row = db.execute(text("INSERT INTO purchases (customer_id) VALUES (:cid) RETURNING purchase_id,customer_id,purchase_date,purchase_status,updated_at"),
                         {"cid": cid}).mappings().first()
        db.commit()
    except IntegrityError:
        db.rollback()
        open_row = _get_open_with_customer(db)
        if open_row:
            cust = _get_customer_by_id(db, open_row["customer_id"]) if open_row["customer_id"] else None
            return PurchaseOut(
                purchase_id=open_row["purchase_id"],
                customer_id=open_row["customer_id"],
                purchase_date=open_row["purchase_date"],
                purchase_status=open_row["purchase_status"],
                updated_at=open_row["updated_at"],
                customer_name=open_row["customer_name"],
                customer_national_id=(cust["customer_national_id"] if cust else None),
                customer_address=(cust["customer_address"] if cust else None),
                photo_path=photo_path or _get_latest_customer_photo(db, open_row["customer_id"]),
                resumed=True,
                notice="พบใบ OPEN ค้าง",
            )
        raise HTTPException(status_code=409, detail="ไม่สามารถเปิดบิลใหม่ได้")

    cust = _get_customer_by_id(db, cid)
    return PurchaseOut(
        purchase_id=row["purchase_id"],
        customer_id=row["customer_id"],
        purchase_date=row["purchase_date"],
        purchase_status=row["purchase_status"],
        updated_at=row["updated_at"],
        customer_name=(cust["customer_name"] if cust else None),
        customer_national_id=(cust["customer_national_id"] if cust else None),
        customer_address=(cust["customer_address"] if cust else None),
        photo_path=photo_path or _get_latest_customer_photo(db, cid),
        resumed=False,
        notice="เปิดบิลใหม่เรียบร้อย (idcard)",
    )

# ====== quick-open existing ======
@router.post("/quick-open/existing", response_model=PurchaseOut, status_code=201)
def quick_open_existing(
    customer_id: int = Query(...),
    on_open: str = Query("return"),
    confirm_delete: bool = Query(False),
    db: Session = Depends(get_db),
):
    # ตรวจว่าลูกค้ามีจริง
    cust = _get_customer_by_id(db, customer_id)
    if not cust:
        raise HTTPException(status_code=404, detail="Customer not found")

    # ถ้ามีใบ OPEN อยู่แล้ว
    open_row = _get_open_with_customer(db)
    if open_row:
        if on_open == "return":
            return PurchaseOut(
                purchase_id=open_row["purchase_id"],
                customer_id=open_row["customer_id"],
                purchase_date=open_row["purchase_date"],
                purchase_status=open_row["purchase_status"],
                updated_at=open_row["updated_at"],
                customer_name=open_row["customer_name"],
                customer_national_id=cust["customer_national_id"],
                customer_address=cust["customer_address"],
                photo_path=_get_latest_customer_photo(db, open_row["customer_id"]),
                resumed=True,
                notice="มีบิลค้างอยู่",
            )
        if on_open == "delete_then_new":
            if not confirm_delete:
                raise HTTPException(status_code=400, detail="ต้อง confirm_delete=true")
            db.execute(text("DELETE FROM purchases WHERE purchase_id=:pid AND purchase_status='OPEN'"),
                       {"pid": open_row["purchase_id"]})
            db.commit()
        if on_open == "error":
            raise HTTPException(status_code=409, detail="มีบิลค้างอยู่")

    # เปิดบิลใหม่
    try:
        row = db.execute(text("INSERT INTO purchases (customer_id) VALUES (:cid) RETURNING purchase_id,customer_id,purchase_date,purchase_status,updated_at"),
                         {"cid": customer_id}).mappings().first()
        db.commit()
    except IntegrityError:
        db.rollback()
        open_row = _get_open_with_customer(db)
        if open_row:
            return PurchaseOut(
                purchase_id=open_row["purchase_id"],
                customer_id=open_row["customer_id"],
                purchase_date=open_row["purchase_date"],
                purchase_status=open_row["purchase_status"],
                updated_at=open_row["updated_at"],
                customer_name=open_row["customer_name"],
                customer_national_id=cust["customer_national_id"],
                customer_address=cust["customer_address"],
                photo_path=_get_latest_customer_photo(db, open_row["customer_id"]),
                resumed=True,
                notice="พบใบ OPEN ค้าง",
            )
        raise HTTPException(status_code=409, detail="ไม่สามารถเปิดบิลใหม่ได้")

    return PurchaseOut(
        purchase_id=row["purchase_id"],
        customer_id=row["customer_id"],
        purchase_date=row["purchase_date"],
        purchase_status=row["purchase_status"],
        updated_at=row["updated_at"],
        customer_name=cust["customer_name"],
        customer_national_id=cust["customer_national_id"],
        customer_address=cust["customer_address"],
        photo_path=_get_latest_customer_photo(db, customer_id),
        resumed=False,
        notice="เปิดบิลใหม่เรียบร้อย (existing)",
    )
