# backend/app/routers/purchase_items.py
from decimal import Decimal, ROUND_HALF_UP, ROUND_UP, ROUND_DOWN
from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File, Response
from pydantic import BaseModel, Field
from typing import Optional, List
from sqlalchemy.orm import Session
from sqlalchemy import text
from app.database import SessionLocal
import os, uuid, shutil
import requests
from pathlib import Path
import re


router = APIRouter(prefix="/purchases", tags=["purchase_items"])

# ---------- CONFIG ----------
UPLOAD_ROOT = os.getenv("UPLOAD_ROOT", "/app/app/uploads")
ITEM_DIR = os.path.join(UPLOAD_ROOT, "purchase_items")
os.makedirs(ITEM_DIR, exist_ok=True)

HARDWARE_URL = os.getenv("HARDWARE_URL", "http://host.docker.internal:9000")
ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".webp"}

ENV_CAMERA_INDEX = os.getenv("CAMERA_DEVICE_INDEX")  # เช่น "1"
ENV_CAMERA_NAME_REGEX = os.getenv("CAMERA_NAME_REGEX")  # เช่น ".*(USB|Logitech|UVC).*"

# ---------- DB Session ----------
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def _list_cameras_from_hardware() -> list[dict]:
    try:
        r = requests.get(f"{HARDWARE_URL}/camera/devices", timeout=5)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list):
            return data
        # เผื่อบาง service ห่อใน key
        if isinstance(data, dict) and "devices" in data and isinstance(data["devices"], list):
            return data["devices"]
    except requests.RequestException:
        pass
    return []

def _pick_usb_camera_index(camera_name_regex: str | None = None) -> int | None:
    """
    เลือก index ของกล้อง USB ตามกติกา:
      1) ถ้ามี ENV_CAMERA_INDEX ให้ใช้ทันที
      2) หากมีรายการอุปกรณ์จาก /camera/devices:
         2.1) ถ้ามี regex ให้เลือกตัวที่ชื่อ match ก่อน
         2.2) มิฉะนั้น เลือกตัวที่ transport == 'usb' หรือ name/path บอกใบ้ว่าเป็น USB/UVC
      3) ถ้าเลือกไม่ได้ คืน None
    """
    # 1) บังคับจาก ENV
    if ENV_CAMERA_INDEX:
        try:
            return int(ENV_CAMERA_INDEX)
        except ValueError:
            pass

    # 2) หาอุปกรณ์จริงจาก hardware_service
    devices = _list_cameras_from_hardware()
    if not devices:
        return None

    # ใช้ regex จาก env ถ้าไม่ส่งมากับฟังก์ชัน
    if camera_name_regex is None:
        camera_name_regex = ENV_CAMERA_NAME_REGEX

    pat = re.compile(camera_name_regex, re.IGNORECASE) if camera_name_regex else None

    # 2.1) เลือกจาก regex ก่อน
    if pat:
        for d in devices:
            name = f"{d.get('name','')} {d.get('path','')}"
            if pat.search(name):
                return int(d.get("index", 0))

    # 2.2) เลือกตัวที่บอกใบ้ว่าเป็น USB
    for d in devices:
        transport = (d.get("transport") or "").lower()
        name = f"{d.get('name','')} {d.get('path','')}".lower()
        if transport == "usb" or "usb" in name or "uvc" in name:
            return int(d.get("index", 0))

    return None        

# ---------- Guards ----------
def ensure_open(db: Session, purchase_id: int):
    st = db.execute(
        text("SELECT purchase_status FROM purchases WHERE purchase_id=:pid"),
        {"pid": purchase_id}
    ).mappings().first()
    if not st:
        raise HTTPException(status_code=404, detail="Purchase not found")
    if st["purchase_status"] != "OPEN":
        raise HTTPException(status_code=409, detail="Purchase is not OPEN")

def _safe_abs_from_imgpath(img_path: str) -> str:
    rel = img_path.replace("/uploads/", "").lstrip("/\\").replace("..", "")
    abs_path = os.path.join(UPLOAD_ROOT, rel)
    abs_path = str(Path(abs_path).resolve())
    if not abs_path.startswith(str(Path(UPLOAD_ROOT).resolve())):
        raise HTTPException(status_code=400, detail="Invalid image path")
    return abs_path

# ---------- Rounding Helper ----------
ROUND_MAP = {
    "half_up": ROUND_HALF_UP,
    "up": ROUND_UP,
    "down": ROUND_DOWN,
}
def _round_money(value: Decimal, mode: str) -> Decimal:
    r = ROUND_MAP.get(mode, ROUND_HALF_UP)
    return value.quantize(Decimal("0.01"), rounding=r)

# ---------- Schemas ----------
class PhotoOut(BaseModel):
    photo_id: int
    img_path: str  # เปิดเป็น /uploads/... ได้เลย

class ItemOut(BaseModel):
    purchase_item_id: int
    purchase_id: int
    prod_id: int
    weight: Decimal
    price: Decimal
    prod_name: Optional[str] = None
    photos: List[PhotoOut] = Field(default_factory=list)

class ItemCreate(BaseModel):
    prod_id: int
    weight: Decimal = Field(ge=0)

class ItemUpdatePrice(BaseModel):
    price: Decimal = Field(ge=0)

class ProductOut(BaseModel):
    prod_id: int
    prod_name: str
    prod_price: Decimal
    is_active: bool
    prod_img: Optional[str] = None

# ---------- Photo Helpers ----------
def _save_item_photos(item_id: int, files: List[UploadFile] | None, db: Session) -> List[PhotoOut]:
    if not files:
        return []
    results: List[PhotoOut] = []
    for f in files:
        if not f or not f.filename:
            continue
        _, ext = os.path.splitext(f.filename or "")
        ext = (ext or "").lower()
        if ext not in ALLOWED_EXT:
            raise HTTPException(status_code=400, detail="รองรับไฟล์เฉพาะ .jpg .jpeg .png .webp")

        fname = f"{uuid.uuid4().hex}{ext}"
        abs_path = os.path.join(ITEM_DIR, fname)
        with open(abs_path, "wb") as out:
            shutil.copyfileobj(f.file, out)

        row = db.execute(
            text("""INSERT INTO purchase_item_photos (purchase_item_id, img_path)
                    VALUES (:iid, :p)
                 RETURNING photo_id, img_path"""),
            {"iid": item_id, "p": f"/uploads/purchase_items/{fname}"}
        ).mappings().first()
        results.append(PhotoOut(photo_id=row["photo_id"], img_path=row["img_path"]))
    return results

def _get_item_photos(db: Session, item_id: int) -> List[PhotoOut]:
    rows = db.execute(
        text("""SELECT photo_id, img_path
                FROM purchase_item_photos
                WHERE purchase_item_id=:iid
                ORDER BY photo_id ASC"""),
        {"iid": item_id}
    ).mappings().all()
    return [PhotoOut(photo_id=r["photo_id"], img_path=r["img_path"]) for r in rows]

# ---------- 0a) ดึงสินค้าที่รับซื้อทั้งหมด (ค้นหา/กรอง/เพจ) ----------
@router.get("/products", response_model=List[ProductOut])
def list_purchase_products(
    q: Optional[str] = Query(None, description="ค้นหาชื่อสินค้า (contains)"),
    include_inactive: bool = Query(False, description="รวมสินค้าที่ปิดใช้งาน"),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    sql = """
        SELECT prod_id, prod_name, prod_price, is_active, prod_img
        FROM product
        WHERE (:include_inactive = TRUE OR COALESCE(is_active, TRUE) = TRUE)
          AND (:q IS NULL OR LOWER(prod_name) LIKE LOWER(:q_like))
        ORDER BY prod_name ASC, prod_id ASC
        LIMIT :limit OFFSET :offset
    """
    rows = db.execute(
        text(sql),
        {
            "include_inactive": include_inactive,
            "q": q,
            "q_like": f"%{q}%" if q else None,
            "limit": limit,
            "offset": offset,
        },
    ).mappings().all()

    out: List[ProductOut] = []
    for r in rows:
        d = dict(r)

        # ถ้าใน DB เก็บเป็นแค่ไฟล์เนม ให้เติม prefix เป็น /uploads/products/ ให้หน้าเว็บโหลดได้
        img = d.get("prod_img")
        if img:
            if img.startswith("/uploads/"):
                d["prod_img"] = img
            else:
                d["prod_img"] = f"/uploads/products/{img}"
        else:
            d["prod_img"] = None  # หรือจะใส่ placeholder ก็ได้

        out.append(ProductOut(**d))
    return out


# ---------- 1) ดูรายการของบิล ----------
@router.get("/{purchase_id}/items", response_model=List[ItemOut])
def list_items(purchase_id: int, db: Session = Depends(get_db)):
    rows = db.execute(text("""
      SELECT i.purchase_item_id, i.purchase_id, i.prod_id, i.weight, i.price,
             p.prod_name
      FROM purchase_items i
      LEFT JOIN product p ON p.prod_id = i.prod_id
      WHERE i.purchase_id = :pid
      ORDER BY i.purchase_item_id ASC
    """), {"pid": purchase_id}).mappings().all()

    out: List[ItemOut] = []
    for r in rows:
        photos = _get_item_photos(db, r["purchase_item_id"])
        out.append(ItemOut(**dict(r), photos=photos))
    return out

# ---------- 2) สรุปยอดของบิล ----------
@router.get("/{purchase_id}/items/summary")
def items_summary(purchase_id: int, db: Session = Depends(get_db)):
    row = db.execute(text("""
      SELECT
        COALESCE(SUM(weight), 0) AS total_weight,
        COALESCE(SUM(price), 0)  AS total_amount
      FROM purchase_items
      WHERE purchase_id = :pid
    """), {"pid": purchase_id}).mappings().first()
    return row

# ---------- 3) เพิ่มรายการ (บังคับถ่ายรูปเสมอ) ----------
@router.post("/{purchase_id}/items", response_model=ItemOut, status_code=201)
def add_item(
    purchase_id: int,
    body: ItemCreate,
    round_mode: str = Query(
        "half_up", pattern="^(half_up|up|down)$",
        description="โหมดปัดราคา: half_up|up|down (เริ่มต้น: half_up)"
    ),
    # เปลี่ยน default เป็น -1 และอนุญาต ge=-1
    device_index: int = Query(
        -1, ge=-1,
        description="index กล้องที่ผู้ใช้เลือก (-1 = ให้ระบบเลือกอัตโนมัติ)"
    ),
    warmup: int = Query(8, ge=0, le=30, description="วอร์มกล้อง"),
    width: int = Query(1280, ge=160, le=3840),
    height: int = Query(720,  ge=120, le=2160),

    auto_pick_usb: bool = Query(True, description="พยายามเลือกกล้อง USB อัตโนมัติเมื่อ device_index=-1"),
    camera_name_regex: Optional[str] = Query(
        None,
        description="regex สำหรับชื่อ/พาธกล้อง เช่น '.*(Logitech|USB).*'"
    ),

    db: Session = Depends(get_db),
):
    ensure_open(db, purchase_id)

    # 1) ดึงข้อมูลสินค้า
    prod = db.execute(
        text("SELECT prod_name, prod_price FROM product WHERE prod_id=:id"),
        {"id": body.prod_id}
    ).mappings().first()
    if not prod:
        raise HTTPException(status_code=400, detail="Product not found")

    # 2) เลือกกล้อง (priority: ค่าที่ผู้ใช้เลือกมาก่อน)
    if device_index >= 0:
        chosen_index = device_index
    else:
        if auto_pick_usb:
            chosen_index = _pick_usb_camera_index(camera_name_regex=camera_name_regex)
            if chosen_index is None:
                chosen_index = -1  # ให้ hardware_service auto
        else:
            # ใช้ ENV_CAMERA_INDEX ถ้ามี ไม่งั้น fallback เป็น 0
            try:
                chosen_index = int(ENV_CAMERA_INDEX) if ENV_CAMERA_INDEX is not None else 0
            except ValueError:
                chosen_index = 0

    # 3) ถ่ายภาพจาก hardware_service
    try:
        r = requests.post(
            f"{HARDWARE_URL}/camera/capture",
            params={
                "device_index": chosen_index,   # อาจเป็นค่าที่ผู้ใช้เลือก หรือ -1 เพื่อ auto
                "warmup": warmup,
                "width": width,
                "height": height,
            },
            timeout=20,
        )
        if r.status_code >= 400:
            try:
                msg = r.json().get("detail")
            except Exception:
                msg = r.text
            raise HTTPException(status_code=502, detail=f"Capture failed: {msg}")
        captured_bytes = r.content
        if not captured_bytes:
            raise HTTPException(status_code=502, detail="No image data from hardware")
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Hardware unreachable: {e}")

    # 4) คำนวณราคา + ปัดตามโหมด
    raw_price = body.weight * prod["prod_price"]
    total_price = _round_money(Decimal(raw_price), round_mode)

    # 5) บันทึกรายการ
    item = db.execute(text("""
        INSERT INTO purchase_items (purchase_id, prod_id, weight, price)
        VALUES (:pid, :prod, :w, :p)
        RETURNING purchase_item_id, purchase_id, prod_id, weight, price
    """), {"pid": purchase_id, "prod": body.prod_id, "w": body.weight, "p": total_price}
    ).mappings().first()

    # 6) บันทึกรูป (บังคับอย่างน้อย 1 รูป)
    fname = f"{uuid.uuid4().hex}.jpg"
    abs_path = os.path.join(ITEM_DIR, fname)
    with open(abs_path, "wb") as f:
        f.write(captured_bytes)

    photo = db.execute(text("""
        INSERT INTO purchase_item_photos (purchase_item_id, img_path)
        VALUES (:iid, :p)
        RETURNING photo_id, img_path
    """), {"iid": item["purchase_item_id"], "p": f"/uploads/purchase_items/{fname}"}).mappings().first()

    db.commit()

    return ItemOut(
        **dict(item),
        prod_name=prod["prod_name"],
        photos=[PhotoOut(photo_id=photo["photo_id"], img_path=photo["img_path"])]
    )



# ---------- 5) แก้ไข "ราคา" เท่านั้น (มีโหมดปัดราคา) ----------
@router.put("/{purchase_id}/items/{item_id}", response_model=ItemOut)
def update_item_price_only(
    purchase_id: int,
    item_id: int,
    body: ItemUpdatePrice,
    round_mode: str = Query("half_up", pattern="^(half_up|up|down)$",
                            description="โหมดปัดราคา: half_up|up|down (เริ่มต้น: half_up)"),
    db: Session = Depends(get_db),
):
    ensure_open(db, purchase_id)

    cur = db.execute(text("""
        SELECT i.purchase_item_id, i.purchase_id, i.prod_id, i.weight, i.price, p.prod_name
        FROM purchase_items i
        LEFT JOIN product p ON p.prod_id = i.prod_id
        WHERE i.purchase_id=:pid AND i.purchase_item_id=:iid
    """), {"pid": purchase_id, "iid": item_id}).mappings().first()
    if not cur:
        raise HTTPException(status_code=404, detail="Item not found")

    new_price = _round_money(body.price, round_mode)

    row = db.execute(text("""
        UPDATE purchase_items
           SET price = :p
         WHERE purchase_id=:pid AND purchase_item_id=:iid
     RETURNING purchase_item_id, purchase_id, prod_id, weight, price
    """), {"pid": purchase_id, "iid": item_id, "p": new_price}
    ).mappings().first()
    db.commit()

    photos = _get_item_photos(db, item_id)
    return ItemOut(**dict(row), prod_name=cur["prod_name"], photos=photos)

# ---------- 6) ลบรายการ ----------
@router.delete("/{purchase_id}/items/{item_id}", status_code=204)
def delete_purchase_item(
    purchase_id: int,
    item_id: int,
    db: Session = Depends(get_db),
):
    ensure_open(db, purchase_id)

    photos = db.execute(
        text("SELECT img_path FROM purchase_item_photos WHERE purchase_item_id=:iid"),
        {"iid": item_id}
    ).mappings().all()

    db.execute(
        text("DELETE FROM purchase_item_photos WHERE purchase_item_id=:iid"),
        {"iid": item_id}
    )
    row = db.execute(
        text("DELETE FROM purchase_items WHERE purchase_id=:pid AND purchase_item_id=:iid"),
        {"pid": purchase_id, "iid": item_id}
    )
    db.commit()

    if row.rowcount == 0:
        raise HTTPException(status_code=404, detail="Item not found")

    for p in photos:
        try:
            abs_path = _safe_abs_from_imgpath(p["img_path"])
            if os.path.isfile(abs_path):
                os.remove(abs_path)
        except Exception:
            pass

    return Response(status_code=204)


