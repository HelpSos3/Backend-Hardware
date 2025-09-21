# backend/app/routers/purchases.py
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse , StreamingResponse

from pydantic import BaseModel
from typing import Optional
from sqlalchemy.orm import Session
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from datetime import datetime
import httpx
from app.database import SessionLocal
import os, uuid, requests ,base64 



router = APIRouter(prefix="/purchases", tags=["purchases"])

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ====== ADD: config สำหรับรูปและ hardware ======
UPLOAD_ROOT = os.getenv("UPLOAD_ROOT", "/app/app/uploads")  # ต้องตรงกับ static mount
HARDWARE_URL = os.getenv("HARDWARE_URL", "http://host.docker.internal:9000")
os.makedirs(os.path.join(UPLOAD_ROOT, "customer_photos"), exist_ok=True)

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
    photo_path: Optional[str] = None
    resumed: bool = False
    notice: Optional[str] = None    

# -------- Helpers --------
def _get_open_with_customer(db: Session):
    row = db.execute(
        text("""
        SELECT p.purchase_id, p.customer_id, p.purchase_date, p.purchase_status, p.updated_at,
               c.full_name AS customer_name
        FROM purchases p
        LEFT JOIN customers c ON c.customer_id = p.customer_id
        WHERE p.purchase_status = 'OPEN'
        ORDER BY p.updated_at DESC
        LIMIT 1
        """)
    ).mappings().first()
    return row

# -------- API --------
@router.get("/open", response_model=Optional[PurchaseOut])
def get_open_purchase(db: Session = Depends(get_db)):
    row = _get_open_with_customer(db)
    if not row:
        return None
    out = dict(row)
    out["resumed"] = True
    out["notice"] = f"มีบิลค้างอยู่ (เลขที่ {row['purchase_id']}) ของลูกค้า: {row['customer_name'] or 'ไม่ระบุ'}"
    return out

# ====== ADD: endpoint ปุ่มเดียวจบ ======
@router.post("/quick-open/anonymous", response_model=PurchaseOut, status_code=201)
def quick_open_anonymous(
    device_index: int = Query(0, description="index กล้อง 0,1,..."),
    warmup: int = Query(8, ge=0, le=30, description="จำนวนเฟรมวอร์มกล้อง"),
    on_open: str = Query(
        "return",
        description="ถ้ามีใบ OPEN อยู่แล้ว: return | delete_then_new | error"
    ),
    confirm_delete: bool = Query(
        False,
        description="ต้อง true เมื่อ on_open=delete_then_new เพื่อกันเผลอลบ"
    ),
    db: Session = Depends(get_db),
):
    """
    ทำ 3 ขั้นตอนในปุ่มเดียว:
      1) ถ่ายรูปจาก hardware_service
      2) สร้างลูกค้า anonymous + บันทึกรูป (customer_photos)
      3) เปิดบิลใหม่ให้ลูกค้าคนนั้น

    การจัดการกรณีมีใบ OPEN ค้าง:
      - on_open=return          -> คืนใบเดิม (ไม่ถ่าย/ไม่สร้างใหม่)   -> 200 OK
      - on_open=delete_then_new -> ลบใบเดิมแล้วทำใหม่ (ต้อง confirm_delete=true) -> 201
      - on_open=error           -> 409 พร้อมข้อความ
    """
    # 0) เช็คใบ OPEN ค้างก่อน
    open_row = _get_open_with_customer(db)
    if open_row:
        cust_name = open_row["customer_name"] or "ไม่ระบุ"
        msg = f"มีบิลค้างอยู่ (เลขที่ {open_row['purchase_id']}) ของลูกค้า: {cust_name}"

        if on_open == "return":
            # คืนใบเดิม (ไม่ถ่ายรูป/ไม่สร้างลูกค้าใหม่)
            return PurchaseOut(
                purchase_id=open_row["purchase_id"],
                customer_id=open_row["customer_id"],
                purchase_date=open_row["purchase_date"],
                purchase_status=open_row["purchase_status"],
                updated_at=open_row["updated_at"],
                customer_name=open_row["customer_name"],
                photo_path=None,
                resumed=True,
                notice=msg,
            )

        if on_open == "delete_then_new":
            if not confirm_delete:
                raise HTTPException(status_code=400, detail="ต้องส่ง confirm_delete=true เมื่อต้องการลบบิลเดิม")
            db.execute(
                text("DELETE FROM purchases WHERE purchase_id=:pid AND purchase_status='OPEN'"),
                {"pid": open_row["purchase_id"]},
            )
            db.commit()

        if on_open == "error":
            raise HTTPException(status_code=409, detail=msg)

    # 1) ถ่ายรูปจาก hardware_service
    try:
        r = requests.post(
            f"{HARDWARE_URL}/camera/capture",
            params={"device_index": device_index, "warmup": warmup},
            timeout=15,
        )
        r.raise_for_status()
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Hardware unreachable: {e}")
    img_bytes = r.content

    # 2) เซฟไฟล์รูป + สร้างลูกค้า anonymous + ผูกรูป
    fname = f"{uuid.uuid4().hex}.jpg"
    rel_path = f"customer_photos/{fname}"
    abs_path = os.path.join(UPLOAD_ROOT, rel_path)
    with open(abs_path, "wb") as f:
        f.write(img_bytes)

    row_cust = db.execute(
        text("""
        INSERT INTO customers (full_name, national_id, address)
        VALUES (NULL, NULL, NULL)
        RETURNING customer_id
        """)
    ).mappings().first()
    cid = row_cust["customer_id"]

    db.execute(
        text("""
        INSERT INTO customer_photos (customer_id, photo_path)
        VALUES (:cid, :p)
        """),
        {"cid": cid, "p": f"/uploads/{rel_path}"},
    )

    # 3) เปิดบิลให้ลูกค้าใหม่คนนั้น
    try:
        row = db.execute(
            text("""
                INSERT INTO purchases (customer_id)
                VALUES (:cid)
                RETURNING purchase_id, customer_id, purchase_date, purchase_status, updated_at
            """),
            {"cid": cid},
        ).mappings().first()
        db.commit()
    except IntegrityError:
        # กัน race กับ constraint "OPEN ได้ใบเดียว"
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
                photo_path=f"/uploads/{rel_path}",
                resumed=True,
                notice="พบใบ OPEN ค้าง ระบบพากลับไปที่ใบเดิม",
            )
        raise HTTPException(status_code=409, detail="ไม่สามารถเปิดบิลใหม่ได้ โปรดลองอีกครั้ง")

    # สำเร็จ -> ใบใหม่
    cust = None
    if row["customer_id"] is not None:
        cust = db.execute(
            text("SELECT full_name AS customer_name FROM customers WHERE customer_id=:cid"),
            {"cid": row["customer_id"]},
        ).mappings().first()

    return PurchaseOut(
        purchase_id=row["purchase_id"],
        customer_id=row["customer_id"],
        purchase_date=row["purchase_date"],
        purchase_status=row["purchase_status"],
        updated_at=row["updated_at"],
        customer_name=cust["customer_name"] if cust else None,
        photo_path=f"/uploads/{rel_path}",
        resumed=False,
        notice="เปิดบิลใหม่เรียบร้อย (anonymous + ถ่ายรูปแล้ว)",
    )

# ====== Helper: upsert ลูกค้าตาม national_id ======
def _upsert_customer_by_idcard(db: Session, full_name: str, national_id: str, address: str | None):
    # มีลูกค้าที่ national_id นี้อยู่แล้วไหม
    exist = db.execute(
        text("SELECT customer_id FROM customers WHERE national_id = :nid"),
        {"nid": national_id},
    ).mappings().first()
    if exist:
        cid = exist["customer_id"]
        # อัปเดตชื่อ/ที่อยู่เบา ๆ (จะอัปหรือไม่อัปก็ได้; ที่นี่เลือกอัปเดตเมื่อมีค่านอกเหนือ None/ว่าง)
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
    # ไม่มี → สร้างใหม่
    row = db.execute(
        text("""
            INSERT INTO customers (full_name, national_id, address)
            VALUES (:full_name, :national_id, :address)
            RETURNING customer_id
        """),
        {"full_name": full_name, "national_id": national_id, "address": address},
    ).mappings().first()
    return row["customer_id"]

# ====== Helper: บันทึกรูปจาก base64 (ถ้ามี) และผูกกับลูกค้า ======
def _save_idcard_photo_if_present(db: Session, customer_id: int, photo_b64: str | None) -> str | None:
    if not photo_b64:
        return None
    fname = f"{uuid.uuid4().hex}.jpg"
    rel_path = f"idcard_photos/{fname}"
    abs_path = os.path.join(UPLOAD_ROOT, rel_path)
    with open(abs_path, "wb") as f:
        f.write(base64.b64decode(photo_b64))
    # ผูกกับ customer_photos
    db.execute(
        text("""
            INSERT INTO customer_photos (customer_id, photo_path)
            VALUES (:cid, :p)
        """),
        {"cid": customer_id, "p": f"/uploads/{rel_path}"}
    )
    return f"/uploads/{rel_path}"

# ====== NEW ENDPOINT: ปุ่มเดียวจบ (สแกนบัตร + upsert ลูกค้า + เปิดบิล) ======
@router.post("/quick-open/idcard", response_model=PurchaseOut, status_code=201)
def quick_open_with_idcard(
    reader_index: int = Query(0, ge=0, description="index ของเครื่องอ่านบัตร"),
    with_photo: int = Query(1, ge=0, le=1, description="1=ขอรูปจากบัตรด้วย"),
    on_open: str = Query("return", description="เมื่อเจอใบ OPEN: return | delete_then_new | error"),
    confirm_delete: bool = Query(False, description="ต้องเป็น true ถ้า on_open=delete_then_new"),
    db: Session = Depends(get_db),
):
    """
    1) เรียก hardware_service /idcard/scan
    2) upsert ลูกค้าโดยอ้าง national_id
    3) บันทึกรูปจากบัตร (ถ้ามี)
    4) เปิดบิลให้ลูกค้าคนนั้น
    """

    # 0) ใบ OPEN ค้าง?
    open_row = _get_open_with_customer(db)
    if open_row:
        cust_name = open_row["customer_name"] or "ไม่ระบุ"
        msg = f"มีบิลค้างอยู่ (เลขที่ {open_row['purchase_id']}) ของลูกค้า: {cust_name}"

        if on_open == "return":
            return PurchaseOut(
                purchase_id=open_row["purchase_id"],
                customer_id=open_row["customer_id"],
                purchase_date=open_row["purchase_date"],
                purchase_status=open_row["purchase_status"],
                updated_at=open_row["updated_at"],
                customer_name=open_row["customer_name"],
                photo_path=None,
                resumed=True,
                notice=msg,
            )
        if on_open == "delete_then_new":
            if not confirm_delete:
                raise HTTPException(status_code=400, detail="ต้องส่ง confirm_delete=true เมื่อต้องการลบบิลค้าง")
            db.execute(
                text("DELETE FROM purchases WHERE purchase_id=:pid AND purchase_status='OPEN'"),
                {"pid": open_row["purchase_id"]},
            )
            db.commit()
        if on_open == "error":
            raise HTTPException(status_code=409, detail=msg)

    # 1) เรียก hardware_service เพื่อสแกนบัตร
    try:
        r = requests.get(
            f"{HARDWARE_URL}/idcard/scan",
            params={"reader_index": reader_index, "with_photo": with_photo},
            timeout=30,
        )
        r.raise_for_status()
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"hardware error: {e}")

    payload = r.json()
    national_id = (payload.get("national_id") or "").strip()
    full_name   = (payload.get("full_name") or "").strip()
    address     = payload.get("address") or None
    if not national_id:
        raise HTTPException(status_code=400, detail="ไม่พบ national_id จากบัตร")

    # 2) upsert ลูกค้า
    cid = _upsert_customer_by_idcard(db, full_name=full_name, national_id=national_id, address=address)

    # 3) บันทึกรูปจากบัตร (ถ้ามี)
    photo_path = None
    if with_photo and payload.get("photo_base64"):
        photo_path = _save_idcard_photo_if_present(db, cid, payload["photo_base64"])

    # 4) เปิดบิล
    try:
        row = db.execute(
            text("""
                INSERT INTO purchases (customer_id)
                VALUES (:cid)
                RETURNING purchase_id, customer_id, purchase_date, purchase_status, updated_at
            """),
            {"cid": cid},
        ).mappings().first()
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
                photo_path=photo_path,
                resumed=True,
                notice="พบใบ OPEN ค้าง ระบบพากลับไปที่ใบเดิม",
            )
        raise HTTPException(status_code=409, detail="ไม่สามารถเปิดบิลใหม่ได้ โปรดลองอีกครั้ง")

    # เติมชื่อเพื่อสะดวก FE
    cust = db.execute(
        text("SELECT full_name AS customer_name FROM customers WHERE customer_id=:cid"),
        {"cid": cid},
    ).mappings().first()

    return PurchaseOut(
        purchase_id=row["purchase_id"],
        customer_id=row["customer_id"],
        purchase_date=row["purchase_date"],
        purchase_status=row["purchase_status"],
        updated_at=row["updated_at"],
        customer_name=cust["customer_name"] if cust else None,
        photo_path=photo_path,
        resumed=False,
        notice="เปิดบิลใหม่เรียบร้อย (สแกนบัตร + ผูกรูปแล้ว)",
    )

@router.post("/quick-open/existing", response_model=PurchaseOut, status_code=200)
def quick_open_existing(
    customer_id: int = Query(..., description="customer_id ของลูกค้าที่เลือก"),
    on_open: str = Query("return", description="เมื่อเจอใบ OPEN: return | delete_then_new | error"),
    confirm_delete: bool = Query(False, description="ต้องส่ง true ถ้า on_open=delete_then_new"),
    db: Session = Depends(get_db),
):
    # 0) ตรวจว่าลูกค้ามีจริง
    cust = db.execute(text("""
        SELECT customer_id, full_name AS customer_name
        FROM customers
        WHERE customer_id = :cid
            AND full_name IS NOT NULL
            AND national_id IS NOT NULL
"""), {"cid": customer_id}).mappings().first()
    if not cust:
        raise HTTPException(status_code=404, detail="Customer not found")

    # 1) ถ้ามีใบ OPEN ค้าง จัดการตาม on_open
    open_row = _get_open_with_customer(db)
    if open_row:
        msg = f"มีบิลค้างอยู่ (เลขที่ {open_row['purchase_id']}) ของลูกค้า: {open_row['customer_name'] or 'ไม่ระบุ'}"
        if on_open == "return":
            # คืนใบเดิม (200 OK)
            return PurchaseOut(
                purchase_id=open_row["purchase_id"],
                customer_id=open_row["customer_id"],
                purchase_date=open_row["purchase_date"],
                purchase_status=open_row["purchase_status"],
                updated_at=open_row["updated_at"],
                customer_name=open_row["customer_name"],
                photo_path=None,
                resumed=True,
                notice=msg,
            )
        if on_open == "delete_then_new":
            if not confirm_delete:
                raise HTTPException(status_code=400, detail="ต้องส่ง confirm_delete=true เมื่อต้องการลบบิลค้าง")
            db.execute(
                text("DELETE FROM purchases WHERE purchase_id=:pid AND purchase_status='OPEN'"),
                {"pid": open_row["purchase_id"]},
            )
            db.commit()
        if on_open == "error":
            raise HTTPException(status_code=409, detail=msg)

    # 2) เปิดบิลใหม่ให้ customer_id นี้
    try:
        row = db.execute(text("""
          INSERT INTO purchases (customer_id)
          VALUES (:cid)
          RETURNING purchase_id, customer_id, purchase_date, purchase_status, updated_at
        """), {"cid": customer_id}).mappings().first()
        db.commit()
    except IntegrityError:
        db.rollback()
        # กัน race กับ constraint “OPEN ได้ใบเดียว”
        open_row = _get_open_with_customer(db)
        if open_row:
            return PurchaseOut(
                purchase_id=open_row["purchase_id"],
                customer_id=open_row["customer_id"],
                purchase_date=open_row["purchase_date"],
                purchase_status=open_row["purchase_status"],
                updated_at=open_row["updated_at"],
                customer_name=open_row["customer_name"],
                photo_path=None,
                resumed=True,
                notice="พบใบ OPEN ค้าง ระบบพากลับไปที่ใบเดิม",
            )
        raise HTTPException(status_code=409, detail="ไม่สามารถเปิดบิลใหม่ได้ โปรดลองอีกครั้ง")

    out = PurchaseOut(
        purchase_id=row["purchase_id"],
        customer_id=row["customer_id"],
        purchase_date=row["purchase_date"],
        purchase_status=row["purchase_status"],
        updated_at=row["updated_at"],
        customer_name=cust["customer_name"],
        photo_path=None,
        resumed=False,
        notice="เปิดบิลใหม่เรียบร้อย (ลูกค้าที่มีอยู่)",
    )
    # สร้างใหม่จริง → 201
    return JSONResponse(status_code=201, content=out.dict())

@router.get("/camera/preview")
def camera_preview(
    device_index: int = Query(0, ge=0),
    width: int = Query(1280, ge=160, le=3840),
    height: int = Query(720, ge=120, le=2160),
    warmup: int = Query(8, ge=0, le=60),
    fps: int = Query(15, ge=1, le=60),
):
    
    url = f"{HARDWARE_URL}/camera/preview"
    params = {"device_index": device_index, "width": width, "height": height, "warmup": warmup, "fps": fps}

    client = httpx.Client(timeout=None, follow_redirects=True)
    try:
        r = client.build_request("GET", url, params=params)
        resp = client.send(r, stream=True)
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"hardware preview error: {e}")

    if resp.status_code != 200:
        # อ่านสั้น ๆ กัน block
        detail = resp.text[:200] if resp.text else f"status {resp.status_code}"
        raise HTTPException(status_code=502, detail=f"hardware preview bad response: {detail}")

    def stream():
        try:
            for chunk in resp.iter_raw():
                if chunk:
                    yield chunk
        finally:
            resp.close()
            client.close()

    return StreamingResponse(stream(), media_type="multipart/x-mixed-replace; boundary=frame")