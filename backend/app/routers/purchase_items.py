# backend/app/routers/purchase_items.py
from decimal import Decimal, ROUND_HALF_UP, ROUND_UP, ROUND_DOWN , InvalidOperation
from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File, Response
from pydantic import BaseModel, Field , BeforeValidator
from typing import Optional, List , Annotated
from sqlalchemy.orm import Session
from sqlalchemy import text
from app.database import SessionLocal
import os, uuid, shutil
import requests
from pathlib import Path
from fastapi.responses import JSONResponse
from fastapi.encoders import jsonable_encoder

router = APIRouter(prefix="/purchases", tags=["purchase_items"])

# ---------- CONFIG ----------
UPLOAD_ROOT = os.getenv("UPLOAD_ROOT", "/app/app/uploads")
ITEM_DIR = os.path.join(UPLOAD_ROOT, "purchase_items")
os.makedirs(ITEM_DIR, exist_ok=True)

HARDWARE_URL = os.getenv("HARDWARE_URL", "http://host.docker.internal:9000")
ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".webp"}

HARDWARE_CAM_IDX = int(os.getenv("HARDWARE_CAM_IDX", "0"))
HARDWARE_CAM_BACKEND = os.getenv("HARDWARE_CAM_BACKEND", "auto")

# ---------- DB Session ----------
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

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

def _round_money_step_2dp(
    value: Decimal,
    mode: str = "half_up",
    step: Decimal | None = None,
) -> Decimal:
    v = Decimal(value)

    # โหมดไม่ปัด: ตัดทศนิยมให้เหลือ 2 ตำแหน่ง
    if mode == "none":
        return v.quantize(Decimal("0.01"), rounding=ROUND_DOWN)

    r = ROUND_MAP.get(mode, ROUND_HALF_UP)

    if step is not None:
        step = Decimal(step)
        if step <= 0:
            raise HTTPException(status_code=400, detail="round_step ต้องมากกว่า 0")
        # ต้องเป็นเท่าของ 0.01
        if (step * 100) != (step * 100).to_integral_value():
            raise HTTPException(status_code=400, detail="round_step ต้องเป็นเท่าของ 0.01 (เช่น 0.25, 0.50, 1.00)")
        # ปัดเข้า step ตามโหมด
        v = (v / step).quantize(Decimal("1"), rounding=r) * step

    # สุดท้าย บังคับ 2 ตำแหน่งตามโหมด
    return v.quantize(Decimal("0.01"), rounding=r)
    
def _to_decimal(v):
    if v is None:
        return None
    if isinstance(v, Decimal):
        return v
    try:
        return Decimal(str(v))  # ป้องกัน float เพี้ยน
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f"Invalid decimal value: {v}")

Dec = Annotated[Decimal, BeforeValidator(_to_decimal)]    
# ---------- Schemas ----------
class PhotoOut(BaseModel):
    photo_id: int
    img_path: str  # เปิดเป็น /uploads/... ได้เลย

class ItemOut(BaseModel):
    purchase_item_id: int
    purchase_id: int
    prod_id: int
    weight: Dec
    price: Dec
    prod_name: Optional[str] = None
    photos: List[PhotoOut] = Field(default_factory=list)

class ItemCreate(BaseModel):
    prod_id: int
    weight: Dec = Field(ge=Decimal("0"))

class ItemUpdatePrice(BaseModel):
    price: Dec = Field(ge=Decimal("0"))

class ProductOut(BaseModel):
    prod_id: int
    prod_name: str
    prod_price: Dec
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
        img = d.get("prod_img")
        if img:
            if img.startswith("/uploads/"):
                d["prod_img"] = img
            else:
                d["prod_img"] = f"/uploads/products/{img}"
        else:
            d["prod_img"] = None
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
    # ✅ ปลอดภัยกับ Decimal
    return JSONResponse(content=jsonable_encoder(dict(row), custom_encoder={Decimal: str}))


@router.post("/{purchase_id}/items", response_model=ItemOut, status_code=201)
def add_item(
    purchase_id: int,
    body: ItemCreate,
    round_mode: str = Query(
        "half_up",
        pattern="^(half_up|up|down|none)$",
        description="โหมดปัดราคา: half_up|up|down|none (เริ่มต้น: half_up)"
    ),
    round_step: Optional[Dec] = Query(
        None,
        description="ช่วงปัด (step) เช่น 0.25, 0.50, 1.00; ว่าง = ไม่ใช้ขั้น"
    ),
    # อนุญาต override กล้องจาก frontend ได้ (ไม่ส่งมาก็ใช้ค่า default จาก env)
    cam_idx: Optional[int] = Query(None, ge=0, description="camera index"),
    cam_backend: Optional[str] = Query(None, description="camera backend (auto|dshow|msmf|v4l2|avfoundation)"),
    db: Session = Depends(get_db),
):
    ensure_open(db, purchase_id)

    # 1) ดึงสินค้า
    prod = db.execute(
        text("SELECT prod_name, prod_price FROM product WHERE prod_id=:id"),
        {"id": body.prod_id}
    ).mappings().first()
    if not prod:
        raise HTTPException(status_code=400, detail="Product not found")

    # 2) เรียก hardware_service /camera/snap ด้วย **GET** และใส่พารามิเตอร์กล้อง
    params = {
        "idx": HARDWARE_CAM_IDX if cam_idx is None else cam_idx,
        "backend": HARDWARE_CAM_BACKEND if not cam_backend else cam_backend,
    }
    try:
        r = requests.get(f"{HARDWARE_URL}/camera/snap", params=params, timeout=20)
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Hardware unreachable: {e}")

    if r.status_code != 200:
        # พยายามดึงข้อความ error จาก JSON ถ้ามี
        msg = None
        try:
            msg = r.json().get("detail")
        except Exception:
            msg = r.text
        raise HTTPException(status_code=502, detail=f"Snap failed: {msg}")

    captured_bytes = r.content
    if not captured_bytes:
        raise HTTPException(status_code=502, detail="No image data from hardware (snap)")

    # 3) คำนวณราคา (ทุกอย่างเป็น Decimal เท่านั้น)
    # prod_price อาจเป็น float/str/Decimal → บังคับแปลง
    unit_price = _to_decimal(prod["prod_price"])
    raw_price = body.weight * unit_price
    total_price = _round_money_step_2dp(raw_price, mode=round_mode, step=round_step)

    # 4) บันทึก DB + ไฟล์แบบ transactional
    fname = f"{uuid.uuid4().hex}.jpg"
    abs_path = os.path.join(ITEM_DIR, fname)

    try:
        # 4.1 insert item
        item = db.execute(text("""
            INSERT INTO purchase_items (purchase_id, prod_id, weight, price)
            VALUES (:pid, :prod, :w, :p)
            RETURNING purchase_item_id, purchase_id, prod_id, weight, price
        """), {"pid": purchase_id, "prod": body.prod_id, "w": body.weight, "p": total_price}
        ).mappings().first()

        # 4.2 save file
        with open(abs_path, "wb") as f:
            f.write(captured_bytes)

        # 4.3 insert photo row
        photo = db.execute(text("""
            INSERT INTO purchase_item_photos (purchase_item_id, img_path)
            VALUES (:iid, :p)
            RETURNING photo_id, img_path
        """), {"iid": item["purchase_item_id"], "p": f"/uploads/purchase_items/{fname}"}).mappings().first()

        db.commit()

    except Exception as e:
        db.rollback()
        # ถ้าไฟล์ถูกสร้างแล้วให้ลบคืน
        try:
            if os.path.exists(abs_path):
                os.remove(abs_path)
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=f"Failed to save item/photo: {e}")

    # 5) ส่งออก (แปลง Decimal เป็น string อัตโนมัติ)
    payload = dict(item)
    payload.update({
        "prod_name": prod["prod_name"],
        "photos": [PhotoOut(photo_id=photo["photo_id"], img_path=photo["img_path"])],
    })
    return JSONResponse(content=jsonable_encoder(payload, custom_encoder={Decimal: str}))

# ---------- 5) แก้ไข "ราคา" เท่านั้น (มีโหมดปัดราคา) ----------
@router.put("/{purchase_id}/items/{item_id}", response_model=ItemOut)
def update_item_price_only(
    purchase_id: int,
    item_id: int,
    body: ItemUpdatePrice,
    round_mode: str = Query(
        "half_up",
        pattern="^(half_up|up|down|none)$",
        description="โหมดปัดราคา: half_up|up|down|none (เริ่มต้น: half_up)"
    ),
    round_step: Optional[Dec] = Query(  # ✅ เปลี่ยนเป็น Dec
        None,
        description="ช่วงปัด (step) เช่น 0.25, 0.50, 1.00; ว่าง = ไม่ใช้ขั้น"
    ),
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

    new_price = _round_money_step_2dp(
        Decimal(body.price),
        mode=round_mode,
        step=round_step,
    )

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
