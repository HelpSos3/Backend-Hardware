# backend/app/routers/purchase_items.py
from decimal import Decimal, ROUND_HALF_UP, ROUND_UP, ROUND_DOWN , InvalidOperation
from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File, Response
from pydantic import BaseModel, Field , BeforeValidator
from typing import Optional, List , Annotated
from sqlalchemy.orm import Session
from sqlalchemy import text
from app.database import SessionLocal
import os, uuid, base64, tempfile, httpx
from pathlib import Path
from fastapi.responses import JSONResponse
from fastapi.encoders import jsonable_encoder
router = APIRouter(prefix="/purchases", tags=["purchase_items"])

# ---------- CONFIG ----------
UPLOAD_ROOT = os.getenv("UPLOAD_ROOT", "/app/uploads")
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

# ===== Models (ตัวอย่างย่อ ถ้ามีอยู่แล้วใช้ของเดิมได้) =====
class PreviewPhotoOut(BaseModel):
    photo_base64: str

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


@router.post("/{purchase_id}/items/preview", response_model=PreviewPhotoOut, status_code=200)
def preview_item_photo(
    purchase_id: int,
    device_index: int = Query(0, ge=0),
    warmup: int = Query(3, ge=0, le=30),
    backend: Optional[str] = Query(None, description="camera backend (auto|dshow|msmf|v4l2|avfoundation)"),
):
    # ยิง hardware_service → /camera/capture
    params = {
        "device_index": device_index,
        "warmup": warmup,
        "width": 1280,     # ใช้ดีฟอลต์ตามที่คุณกำหนดไว้
        "height": 720,
        "fps": 15,
        "codec": "MJPG",
        "fps_strategy": "auto",
    }
    if backend:
        params["backend"] = backend

    try:
        with httpx.Client(timeout=httpx.Timeout(20.0)) as client:
            resp = client.post(f"{HARDWARE_URL}/camera/capture", params=params)
            resp.raise_for_status()
            ct = (resp.headers.get("content-type") or "").lower()
            if "image" not in ct:
                raise HTTPException(status_code=502, detail=f"unexpected content-type from hardware: {ct}")
            img_bytes = resp.content
            if not img_bytes:
                raise HTTPException(status_code=502, detail="No image data from hardware")
    except httpx.ConnectError as e:
        raise HTTPException(status_code=502, detail=f"hardware connect error: {e}")
    except httpx.ReadTimeout:
        raise HTTPException(status_code=504, detail="hardware read timeout")
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"hardware error: {e}")

    b64 = base64.b64encode(img_bytes).decode("ascii")
    return PreviewPhotoOut(photo_base64=b64)


# ============ 2) COMMIT: บันทึกรูป + insert item + insert photo ============
class CommitItemIn(BaseModel):
    prod_id: int
    weight: Decimal
    photo_base64: str

@router.post("/{purchase_id}/items/commit", status_code=201)
def commit_item_with_photo(
    purchase_id: int,
    body: CommitItemIn,
    round_mode: str = Query(
        "half_up",
        pattern="^(half_up|up|down|none)$",
        description="โหมดปัดราคา: half_up|up|down|none (เริ่มต้น: half_up)"
    ),
    round_step: Optional[Decimal] = Query(
        None, description="ช่วงปัด (step) เช่น 0.25, 0.50, 1.00; ว่าง = ไม่ใช้ขั้น"
    ),
    db: Session = Depends(get_db),
):
    ensure_open(db, purchase_id)

    # --- decode base64 → bytes ---
    try:
        img_bytes = base64.b64decode(body.photo_base64, validate=True)
        if not img_bytes:
            raise ValueError("empty image")
    except Exception:
        raise HTTPException(status_code=400, detail="photo_base64 ไม่ถูกต้อง")

    # --- ดึงสินค้า ---
    prod = db.execute(
        text("SELECT prod_name, prod_price FROM product WHERE prod_id=:id"),
        {"id": body.prod_id}
    ).mappings().first()
    if not prod:
        raise HTTPException(status_code=400, detail="Product not found")

    # --- คำนวณราคา ---
    unit_price = _to_decimal(prod["prod_price"])
    raw_price = body.weight * unit_price
    total_price = _round_money_step_2dp(raw_price, mode=round_mode, step=round_step)

    # --- เตรียม path ไฟล์ ---
    target_dir = Path(ITEM_DIR)
    target_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{uuid.uuid4().hex}.jpg"
    abs_path = target_dir / fname
    rel_path = f"/uploads/purchase_items/{fname}"

    # --- บันทึก DB + ไฟล์แบบ transactional ---
    tmp_path: Optional[Path] = None
    try:
        # insert item
        item = db.execute(text("""
            INSERT INTO purchase_items (purchase_id, prod_id, weight, price)
            VALUES (:pid, :prod, :w, :p)
            RETURNING purchase_item_id, purchase_id, prod_id, weight, price
        """), {"pid": purchase_id, "prod": body.prod_id, "w": body.weight, "p": total_price}
        ).mappings().first()

        # write temp → atomic replace
        with tempfile.NamedTemporaryFile(dir=target_dir, delete=False) as tmp:
            tmp.write(img_bytes)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp_path = Path(tmp.name)
        os.replace(tmp_path, abs_path)

        # insert photo row
        photo = db.execute(text("""
            INSERT INTO purchase_item_photos (purchase_item_id, img_path)
            VALUES (:iid, :p)
            RETURNING photo_id, img_path
        """), {"iid": item["purchase_item_id"], "p": str(rel_path)}).mappings().first()

        db.commit()

    except Exception as e:
        db.rollback()
        # เก็บบ้าน: ลบ temp ถ้าเหลือ
        if tmp_path and tmp_path.exists():
            try: tmp_path.unlink()
            except: pass
        # ลบไฟล์หลักถ้าถูกเขียนไปแล้ว
        try:
            if abs_path.exists():
                abs_path.unlink()
        except: pass

        raise HTTPException(status_code=500, detail=f"Failed to save item/photo: {e}")

    # ส่งออก (ให้แปลง Decimal เป็น string ถ้าใช้ JSONResponse)
    out = dict(item)
    out.update({
        "prod_name": prod["prod_name"],
        "photos": [{"photo_id": photo["photo_id"], "img_path": photo["img_path"]}],
    })
    return ItemOut(**out)

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
    round_step: Optional[Dec] = Query(  
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
