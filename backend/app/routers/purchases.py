from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from typing import Optional , List
from sqlalchemy.orm import Session
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from datetime import datetime
import httpx
from app.database import SessionLocal
import os, uuid, requests, base64
import math
from pathlib import Path


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

class PreviewPhotoOut(BaseModel):
    photo_base64: str
    note: str = "preview only - not saved"

class CommitAnonymousIn(BaseModel):
    photo_base64: str
    on_open: str = "return"           # "return" | "delete_then_new" | "error"
    confirm_delete: bool = False

class CustomerListItem(BaseModel):
    customer_id: int
    full_name: Optional[str] = None
    address: Optional[str] = None
    photo_path: Optional[str] = None



class PaginatedCustomers(BaseModel):
    items: List[CustomerListItem]
    total: int
    page: int
    page_size: int
    total_pages: int

class CommitIdCardIn(BaseModel):
    full_name: Optional[str] = None
    national_id: str
    address: Optional[str] = None
    photo_base64: Optional[str] = None
    on_open: str = "return"          # "return" | "delete_then_new" | "error"
    confirm_delete: bool = False
    
           

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


@router.post("/quick-open/idcard/preview", response_model=IdCardPreviewOut)
def idcard_preview(
    reader_index: int = Query(0),
    with_photo: int = Query(1),
):
    try:
        r = requests.get(
            f"{HARDWARE_URL}/idcard/scan",
            params={"reader_index": reader_index, "with_photo": with_photo},
            timeout=30,
        )
        r.raise_for_status()
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"hardware error: {e}")

    p = r.json()  # { full_name, national_id, address, photo_base64? }
    return IdCardPreviewOut(
        full_name=p.get("full_name"),
        national_id=p.get("national_id"),
        address=p.get("address"),
        photo_base64=p.get("photo_base64")
    )

@router.post("/quick-open/idcard/commit", response_model=PurchaseOut, status_code=201)
def idcard_commit(
    payload: CommitIdCardIn,
    db: Session = Depends(get_db),
):
    # ถ้ามีใบ OPEN ค้าง → ทำตามนโยบาย
    open_row = _get_open_with_customer(db)
    if open_row:
        if payload.on_open == "return":
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
        if payload.on_open == "delete_then_new":
            if not payload.confirm_delete:
                raise HTTPException(status_code=400, detail="ต้อง confirm_delete=true")
            db.execute(text(
                "DELETE FROM purchases WHERE purchase_id=:pid AND purchase_status='OPEN'"
            ), {"pid": open_row["purchase_id"]})
            db.commit()
        if payload.on_open == "error":
            raise HTTPException(status_code=409, detail="มีบิลค้างอยู่")

    # ตรวจข้อมูลจาก preview
    nid = (payload.national_id or "").strip()
    if not nid:
        raise HTTPException(status_code=400, detail="ต้องมี national_id")
    full_name = (payload.full_name or "").strip()
    address = (payload.address or None)

    # upsert ลูกค้าตามเลขบัตร
    cid = _upsert_customer_by_idcard(db, full_name, nid, address)

    # บันทึกรูปบัตร ถ้าส่งมา
    photo_path = None
    if payload.photo_base64:
        photo_path = _save_idcard_photo_if_present(db, cid, payload.photo_base64)

    # เปิดบิล
    try:
        row = db.execute(text(
            "INSERT INTO purchases (customer_id) VALUES (:cid) "
            "RETURNING purchase_id,customer_id,purchase_date,purchase_status,updated_at"
        ), {"cid": cid}).mappings().first()
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
        notice="เปิดบิลใหม่เรียบร้อย (idcard via preview/commit)",
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
    if on_open == "return":
        open_cust = _get_customer_by_id(db, open_row["customer_id"]) if open_row["customer_id"] else None
        return PurchaseOut(
            purchase_id=open_row["purchase_id"],
            customer_id=open_row["customer_id"],
            purchase_date=open_row["purchase_date"],
            purchase_status=open_row["purchase_status"],
            updated_at=open_row["updated_at"],
            customer_name=open_row["customer_name"],
            customer_national_id=(open_cust["customer_national_id"] if open_cust else None),
            customer_address=(open_cust["customer_address"] if open_cust else None),
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

@router.post("/quick-open/anonymous/preview", response_model=PreviewPhotoOut)
def quick_open_anonymous_preview(
    device_index: int = Query(0),
    warmup: int = Query(8, ge=0, le=30),
):
    # เรียก hardware_service เพื่อถ่ายภาพ
    try:
        r = requests.post(
            f"{HARDWARE_URL}/camera/capture",
            params={"device_index": device_index, "warmup": warmup},
            timeout=15,
        )
        r.raise_for_status()
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Hardware error: {e}")

    b64 = base64.b64encode(r.content).decode("ascii")
    return PreviewPhotoOut(photo_base64=b64)

@router.post("/quick-open/anonymous/commit", response_model=PurchaseOut, status_code=201)
def quick_open_anonymous_commit(
    payload: CommitAnonymousIn,
    db: Session = Depends(get_db),
):
    # 0) ถ้ามีใบ OPEN อยู่แล้ว -> ทำตาม on_open
    open_row = _get_open_with_customer(db)
    if open_row:
        if payload.on_open == "return":
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
        if payload.on_open == "delete_then_new":
            if not payload.confirm_delete:
                raise HTTPException(status_code=400, detail="ต้องส่ง confirm_delete=true")
            db.execute(text(
                "DELETE FROM purchases WHERE purchase_id=:pid AND purchase_status='OPEN'"),
                {"pid": open_row["purchase_id"]}
            )
            db.commit()
        if payload.on_open == "error":
            raise HTTPException(status_code=409, detail="มีบิลค้างอยู่")

    # 1) บันทึกรูปจาก base64 ลงไฟล์
    try:
        img_bytes = base64.b64decode(payload.photo_base64, validate=True)
    except Exception:
        raise HTTPException(status_code=400, detail="photo_base64 ไม่ถูกต้อง")

    fname = f"{uuid.uuid4().hex}.jpg"
    rel_path = f"customer_photos/{fname}"
    abs_path = os.path.join(UPLOAD_ROOT, rel_path)
    try:
        with open(abs_path, "wb") as f:
            f.write(img_bytes)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"ไม่สามารถบันทึกรูปภาพได้: {e}")

    # 2) ลูกค้า anonymous
    row_cust = db.execute(text(
        "INSERT INTO customers (full_name,national_id,address) VALUES (NULL,NULL,NULL) RETURNING customer_id"
    )).mappings().first()
    cid = row_cust["customer_id"]

    db.execute(text(
        "INSERT INTO customer_photos (customer_id, photo_path) VALUES (:cid,:p)"
    ), {"cid": cid, "p": f"/uploads/{rel_path}"})

    # 3) เปิดบิล
    try:
        row = db.execute(text(
            "INSERT INTO purchases (customer_id) VALUES (:cid) "
            "RETURNING purchase_id,customer_id,purchase_date,purchase_status,updated_at"
        ), {"cid": cid}).mappings().first()
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
        notice="เปิดบิลใหม่เรียบร้อย (anonymous via preview)",
    )

# --------- List + Search + Pagination ----------
@router.get("/customers", response_model=PaginatedCustomers)
def list_customers(
    q: Optional[str] = Query(None, description="คำค้นหา (ชื่อหรือที่อยู่บางส่วน)"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
):
    where = "TRUE"
    params = {}
    if q and q.strip():
        where = "(c.full_name ILIKE :kw OR c.address ILIKE :kw)"
        params["kw"] = f"%{q.strip()}%"

    total = db.execute(
        text(f"SELECT COUNT(*) AS n FROM customers c WHERE {where}"),
        params
    ).scalar_one()

    if total == 0:
        return {"items": [], "total": 0, "page": page, "page_size": page_size, "total_pages": 0}

    offset = (page - 1) * page_size

    sql = f"""
        SELECT 
            c.customer_id,
            c.full_name,
            c.address,
            p.photo_path
        FROM customers c
        LEFT JOIN LATERAL (
            SELECT photo_path
            FROM customer_photos
            WHERE customer_id = c.customer_id
            ORDER BY photo_id DESC
            LIMIT 1
        ) p ON TRUE
        WHERE {where}
        ORDER BY c.full_name NULLS LAST, c.customer_id ASC
        LIMIT :limit OFFSET :offset
    """
    rows = db.execute(
        text(sql),
        {**params, "limit": page_size, "offset": offset}
    ).mappings().all()

    return {
        "items": rows,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": math.ceil(total / page_size),
    }

def _safe_abs_from_imgpath(img_path: str) -> str:
    # แปลง "/uploads/xxx" → absolute ภายใต้ UPLOAD_ROOT เท่านั้น
    rel = img_path.replace("/uploads/", "").lstrip("/\\").replace("..", "")
    abs_path = str(Path(os.path.join(UPLOAD_ROOT, rel)).resolve())
    if not abs_path.startswith(str(Path(UPLOAD_ROOT).resolve())):
        raise HTTPException(status_code=400, detail="Invalid image path")
    return abs_path

@router.delete("/open")
def delete_open_purchase(confirm: bool = Query(False), db: Session = Depends(get_db)):
    if not confirm:
        raise HTTPException(status_code=400, detail="ต้องส่ง confirm=true")

    r = _get_open_with_customer(db)
    if not r:
        raise HTTPException(status_code=404, detail="ไม่พบบิล OPEN")

    pid = r["purchase_id"]

    # 1) ดึงรายการ path ของรูปที่จะลบทิ้ง (เผื่อไปลบไฟล์หลัง commit)
    photo_rows = db.execute(text("""
        SELECT p.img_path
        FROM purchase_item_photos p
        JOIN purchase_items i ON i.purchase_item_id = p.purchase_item_id
        WHERE i.purchase_id = :pid
    """), {"pid": pid}).mappings().all()
    photo_paths = [row["img_path"] for row in photo_rows if row.get("img_path")]

    # 2) ลบรูปของรายการ (DB)
    db.execute(text("""
        DELETE FROM purchase_item_photos
        WHERE purchase_item_id IN (
            SELECT purchase_item_id FROM purchase_items WHERE purchase_id = :pid
        )
    """), {"pid": pid})

    # 3) ลบ “รายการสินค้า” ของบิล (DB)
    db.execute(text("""
        DELETE FROM purchase_items WHERE purchase_id = :pid
    """), {"pid": pid})

    # 4) ลบบิล (DB)
    res = db.execute(text("""
        DELETE FROM purchases
        WHERE purchase_id = :pid AND purchase_status = 'OPEN'
    """), {"pid": pid})

    db.commit()

    if res.rowcount == 0:
        raise HTTPException(status_code=409, detail="ลบบิลไม่สำเร็จ (อาจถูกปิดไปแล้ว)")

    # 5) ลบไฟล์จริงในดิสก์ (นอก transaction)
    for p in photo_paths:
        try:
            abs_path = _safe_abs_from_imgpath(p)
            if os.path.isfile(abs_path):
                os.remove(abs_path)
        except Exception:
            # ไม่ให้ fail ทั้ง API เพราะลบไฟล์บางรูปไม่สำเร็จ
            pass

    return {"ok": True, "deleted_purchase_id": pid, "deleted_items": True, "deleted_item_photos": True}
