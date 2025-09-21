# backend/app/routers/purchase_items.py
from decimal import Decimal, ROUND_HALF_UP
from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File
from pydantic import BaseModel, Field
from typing import Optional, List
from sqlalchemy.orm import Session
from sqlalchemy import text
from app.database import SessionLocal
import os, uuid, shutil
import requests
router = APIRouter(prefix="/purchases", tags=["purchase_items"])

# ---------- CONFIG ----------
UPLOAD_ROOT = os.getenv("UPLOAD_ROOT", "/app/app/uploads")
ITEM_DIR = os.path.join(UPLOAD_ROOT, "purchase_items")
os.makedirs(ITEM_DIR, exist_ok=True)

HARDWARE_URL = os.getenv("HARDWARE_URL", "http://host.docker.internal:9000")
ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".webp"}

def get_db():
    db = SessionLocal()
    try: yield db
    finally: db.close()

def ensure_open(db: Session, purchase_id: int):
    st = db.execute(
        text("SELECT purchase_status FROM purchases WHERE purchase_id=:pid"),
        {"pid": purchase_id}
    ).mappings().first()
    if not st:
        raise HTTPException(status_code=404, detail="Purchase not found")
    if st["purchase_status"] != "OPEN":
        raise HTTPException(status_code=409, detail="Purchase is not OPEN")

# ---------- Schemas ----------
class PhotoOut(BaseModel):
    photo_id: int
    img_path: str  # frontend เปิดเป็น /uploads/... ได้เลย

class ItemOut(BaseModel):
    purchase_item_id: int
    purchase_id: int
    prod_id: int
    weight: Decimal
    price: Decimal
    prod_name: Optional[str] = None
    photos: List[PhotoOut] = []   # ← แนบรูปที่ผูกกับรายการนี้กลับไปด้วย

class ItemCreate(BaseModel):
    prod_id: int
    weight: Decimal = Field(ge=0)

class ItemUpdate(BaseModel):
    price: Optional[Decimal] = Field(default=None, ge=0)

# ---------- Helpers ----------
def _save_item_photos(item_id: int, files: List[UploadFile] | None, db: Session) -> List[PhotoOut]:
    if not files:
        return []
    results: List[PhotoOut] = []
    for f in files:
        if not f or not f.filename:
            continue
        _, ext = os.path.splitext(f.filename)
        ext = (ext or "").lower()
        if ext not in ALLOWED_EXT:
            raise HTTPException(status_code=400, detail="รองรับไฟล์เฉพาะ .jpg .jpeg .png .webp")

        fname = f"{uuid.uuid4().hex}{ext}"
        rel_path = f"purchase_items/{fname}"
        abs_path = os.path.join(UPLOAD_ROOT, rel_path)

        with open(abs_path, "wb") as out:
            shutil.copyfileobj(f.file, out)

        row = db.execute(
            text("""INSERT INTO purchase_item_photos (purchase_item_id, img_path)
                    VALUES (:iid, :p)
                 RETURNING photo_id, img_path"""),
            {"iid": item_id, "p": f"/uploads/{rel_path}"}
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

# ---------- List ----------
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

# ---------- Summary ----------
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


# ---------- Update price only ----------
@router.put("/{purchase_id}/items/{item_id}", response_model=ItemOut)
def update_item_price_only(
    purchase_id: int,
    item_id: int,
    body: ItemUpdate,
    db: Session = Depends(get_db),
):
    ensure_open(db, purchase_id)

    cur = db.execute(text("""
        SELECT i.purchase_item_id, i.prod_id, i.weight, i.price, p.prod_name
        FROM purchase_items i
        LEFT JOIN product p ON p.prod_id = i.prod_id
        WHERE i.purchase_id=:pid AND i.purchase_item_id=:iid
    """), {"pid": purchase_id, "iid": item_id}).mappings().first()
    if not cur:
        raise HTTPException(status_code=404, detail="Item not found")

    row = db.execute(text("""
        UPDATE purchase_items
           SET price = :p
         WHERE purchase_id=:pid AND purchase_item_id=:iid
     RETURNING purchase_item_id, purchase_id, prod_id, weight, price
    """), {"pid": purchase_id, "iid": item_id, "p": body.price}
    ).mappings().first()
    db.commit()

    photos = _get_item_photos(db, item_id)
    return ItemOut(**dict(row), prod_name=cur["prod_name"], photos=photos)

# ---------- Append photos later ----------
# ---------- Create (capture from hardware + save 1 photo) ----------
@router.post("/{purchase_id}/items/capture", response_model=ItemOut, status_code=201)
def add_item_with_capture(
    purchase_id: int,
    prod_id: int = Query(..., description="สินค้า"),
    weight: Decimal = Query(..., ge=0, description="น้ำหนักที่ชั่งได้"),
    device_index: int = Query(0, description="index กล้อง 0,1,..."),
    warmup: int = Query(8, ge=0, le=30, description="วอร์มกล้อง"),
    width: int = Query(1280, ge=160, le=3840),
    height: int = Query(720,  ge=120, le=2160),
    db: Session = Depends(get_db),
):
    ensure_open(db, purchase_id)

    # 1) ดึงราคาต่อหน่วยจาก product
    prod = db.execute(
        text("SELECT prod_name, prod_price FROM product WHERE prod_id=:id"),
        {"id": prod_id}
    ).mappings().first()
    if not prod:
        raise HTTPException(status_code=400, detail="Product not found")

    # 2) เรียก hardware_service จับภาพจากกล้อง
    try:
        r = requests.post(
            f"{HARDWARE_URL}/camera/capture",
            params={
                "device_index": device_index,
                "warmup": warmup,
                "width": width,
                "height": height,
            },
            timeout=15,
        )
        r.raise_for_status()
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Hardware unreachable: {e}")
    img_bytes = r.content

    # 3) คำนวณราคาและสร้างรายการ
    total_price = (weight * prod["prod_price"]).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    item = db.execute(text("""
        INSERT INTO purchase_items (purchase_id, prod_id, weight, price)
        VALUES (:pid, :prod, :w, :p)
        RETURNING purchase_item_id, purchase_id, prod_id, weight, price
    """), {"pid": purchase_id, "prod": prod_id, "w": weight, "p": total_price}
    ).mappings().first()

    # 4) เซฟไฟล์รูป 1 ภาพแล้วผูกกับรายการ
    fname = f"{uuid.uuid4().hex}.jpg"
    rel_path = f"purchase_items/{fname}"
    abs_path = os.path.join(UPLOAD_ROOT, rel_path)
    with open(abs_path, "wb") as f:
        f.write(img_bytes)

    photo = db.execute(text("""
        INSERT INTO purchase_item_photos (purchase_item_id, img_path)
        VALUES (:iid, :p)
        RETURNING photo_id, img_path
    """), {"iid": item["purchase_item_id"], "p": f"/uploads/{rel_path}"}).mappings().first()

    db.commit()

    return ItemOut(
        **dict(item),
        prod_name=prod["prod_name"],
        photos=[PhotoOut(photo_id=photo["photo_id"], img_path=photo["img_path"])]
    )


@router.delete("/{purchase_id}/items/{item_id}", status_code=204)
def delete_purchase_item(
    purchase_id: int,
    item_id: int,
    db: Session = Depends(get_db),
):
    ensure_open(db, purchase_id)

    # ดึงรูปทั้งหมดของ item ก่อน
    photos = db.execute(
        text("SELECT img_path FROM purchase_item_photos WHERE purchase_item_id=:iid"),
        {"iid": item_id}
    ).mappings().all()

    # ลบ row รูปใน DB
    db.execute(
        text("DELETE FROM purchase_item_photos WHERE purchase_item_id=:iid"),
        {"iid": item_id}
    )

    # ลบ item ออกจาก DB
    row = db.execute(
        text("DELETE FROM purchase_items WHERE purchase_id=:pid AND purchase_item_id=:iid"),
        {"pid": purchase_id, "iid": item_id}
    )
    db.commit()

    if row.rowcount == 0:
        raise HTTPException(status_code=404, detail="Item not found")

    # ลบไฟล์จริงในดิสก์
    for p in photos:
        safe_rel = p["img_path"].replace("/uploads/", "").replace("..", "").lstrip("/\\")
        abs_path = os.path.join(UPLOAD_ROOT, safe_rel)
        try:
            if os.path.isfile(abs_path):
                os.remove(abs_path)
        except Exception:
            pass

    return Response(status_code=204)


