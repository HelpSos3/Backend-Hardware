# backend/app/routers/products.py
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Query
from fastapi.responses import Response
from pydantic import BaseModel
from decimal import Decimal
from typing import Optional, List
from sqlalchemy.orm import Session
from sqlalchemy import text
import os, uuid, shutil
from app.database import SessionLocal

router = APIRouter(prefix="/products", tags=["products"])

# ====== CONFIG & HELPERS ======
# ใช้ ENV เป็นแหล่งความจริง (ตั้งใน docker-compose: UPLOAD_ROOT=/app/uploads)
UPLOAD_ROOT = os.getenv("UPLOAD_ROOT", "/app/uploads")
PRODUCT_SUBDIR = "products"
UPLOAD_DIR = os.path.join(UPLOAD_ROOT, PRODUCT_SUBDIR)
os.makedirs(UPLOAD_DIR, exist_ok=True)

ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".webp"}

def _safe_rel(rel_path: Optional[str]) -> Optional[str]:
    if not rel_path:
        return None
    # กัน path traversal
    return rel_path.replace("..", "").lstrip("/\\").replace("\\", "/")

def _abs_from_rel(rel_path: str) -> str:
    safe_rel = _safe_rel(rel_path)
    return os.path.join(UPLOAD_ROOT, safe_rel)

def save_image(file: UploadFile | None) -> Optional[str]:
    """บันทึกรูป -> คืนค่า relative path เช่น 'products/xxxx.jpg'"""
    if not file:
        return None
    _, ext = os.path.splitext(file.filename or "")
    ext = ext.lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(status_code=400, detail="รองรับไฟล์เฉพาะ .jpg .jpeg .png .webp")
    fname = f"{uuid.uuid4().hex}{ext}"
    abs_path = os.path.join(UPLOAD_DIR, fname)
    # เขียนไฟล์
    with open(abs_path, "wb") as out:
        shutil.copyfileobj(file.file, out)
    # คืน path แบบ relative (ให้ frontend ไปประกอบเป็น /uploads/<rel>)
    return f"{PRODUCT_SUBDIR}/{fname}"

def delete_image(rel_path: Optional[str]) -> None:
    """ลบไฟล์รูปจากดิสก์ ถ้ามี เช่น 'products/xxx.jpg'"""
    safe_rel = _safe_rel(rel_path)
    if not safe_rel:
        return
    abs_path = os.path.join(UPLOAD_ROOT, safe_rel)
    try:
        if os.path.isfile(abs_path):
            os.remove(abs_path)
    except Exception:
        # ไม่ให้ API พังเพราะลบไม่สำเร็จ
        pass

# ====== DB session ======
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ====== Schemas ======
class ProductOut(BaseModel):
    prod_id: int
    prod_img: Optional[str] = None
    prod_name: str
    prod_price: Decimal
    category_id: int
    category_name: Optional[str] = None
    is_active: bool

class ProductCreate(BaseModel):
    prod_img: Optional[str] = None
    prod_name: str
    prod_price: Decimal
    category_id: int

class ProductUpdate(BaseModel):
    prod_img: Optional[str] = None
    prod_name: Optional[str] = None
    prod_price: Optional[Decimal] = None
    category_id: Optional[int] = None

class ProductOption(BaseModel):
    value: int   # prod_id
    label: str   # display name

# ====== LIST ======
@router.get("/", response_model=List[ProductOut])
def list_products(
    include_inactive: bool = Query(False, description="แสดงสินค้าที่ปิดใช้งานด้วยหรือไม่"),
    db: Session = Depends(get_db),
):
    sql = """
        SELECT
            p.prod_id,
            p.prod_img,
            p.prod_name,
            p.prod_price,
            p.category_id,
            p.is_active,
            c.category_name
        FROM product p
        LEFT JOIN product_categories c ON c.category_id = p.category_id
        WHERE (:include_inactive = TRUE OR p.is_active = TRUE)
        ORDER BY p.prod_id ASC
    """
    rows = db.execute(text(sql), {"include_inactive": include_inactive}).mappings().all()
    return rows

# ====== CREATE (multipart/form-data รองรับ imageFile) ======
@router.post("/", response_model=ProductOut, status_code=201)
async def create_product(
    prod_name: str = Form(...),
    prod_price: Decimal = Form(...),
    category_id: int = Form(...),
    imageFile: UploadFile | None = File(None),
    db: Session = Depends(get_db),
):
    category = db.execute(
        text("SELECT category_id, category_name FROM product_categories WHERE category_id = :cid"),
        {"cid": category_id},
    ).mappings().first()
    if not category:
        raise HTTPException(status_code=400, detail="Category not found")

    img_rel = save_image(imageFile)

    row = db.execute(
        text("""
            INSERT INTO product (prod_img, prod_name, prod_price, category_id)
            VALUES (:img, :name, :price, :cid)
            RETURNING prod_id, prod_img, prod_name, prod_price, category_id, is_active
        """),
        {"img": img_rel, "name": prod_name.strip(), "price": prod_price, "cid": category_id},
    ).mappings().first()
    db.commit()

    result = dict(row)
    result["category_name"] = category["category_name"]
    return result

# ====== UPDATE (PUT แบบ multipart/form-data) ======
@router.put("/{prod_id}", response_model=ProductOut)
async def update_product(
    prod_id: int,
    prod_name: str = Form(...),
    prod_price: Decimal = Form(...),
    category_id: int = Form(...),
    imageFile: UploadFile | None = File(None),
    db: Session = Depends(get_db),
):
    current = db.execute(text("SELECT * FROM product WHERE prod_id = :pid"), {"pid": prod_id}).mappings().first()
    if not current:
        raise HTTPException(status_code=404, detail="Product not found")

    cat = db.execute(
        text("SELECT category_id, category_name FROM product_categories WHERE category_id = :cid"),
        {"cid": category_id},
    ).mappings().first()
    if not cat:
        raise HTTPException(status_code=400, detail="Category not found")

    new_img_rel = current["prod_img"]
    if imageFile is not None and imageFile.filename:
        new_img_rel = save_image(imageFile)
        try:
            delete_image(current["prod_img"])
        except Exception:
            pass

    row = db.execute(
        text("""
            UPDATE product
               SET prod_img = :img,
                   prod_name = :name,
                   prod_price = :price,
                   category_id = :cid
             WHERE prod_id = :pid
         RETURNING prod_id, prod_img, prod_name, prod_price, category_id , is_active
        """),
        {"img": new_img_rel, "name": prod_name.strip(), "price": prod_price, "cid": category_id, "pid": prod_id},
    ).mappings().first()
    db.commit()

    result = dict(row)
    result["category_name"] = cat["category_name"]
    return result

# ====== PATCH (multipart/form-data รองรับ imageFile) ======
@router.patch("/{prod_id}", response_model=ProductOut)
async def patch_product(
    prod_id: int,
    prod_name: Optional[str] = Form(None),
    prod_price: Optional[Decimal] = Form(None),
    category_id: Optional[int] = Form(None),
    imageFile: UploadFile | None = File(None),
    db: Session = Depends(get_db),
):
    current = db.execute(text("SELECT * FROM product WHERE prod_id = :pid"), {"pid": prod_id}).mappings().first()
    if not current:
        raise HTTPException(status_code=404, detail="Product not found")

    if category_id is not None:
        cat = db.execute(
            text("SELECT category_id FROM product_categories WHERE category_id = :cid"),
            {"cid": category_id},
        ).mappings().first()
        if not cat:
            raise HTTPException(status_code=400, detail="Category not found")

    new_img_rel = None
    if imageFile is not None:
        if imageFile.filename:
            new_img_rel = save_image(imageFile)
            delete_image(current["prod_img"])
        else:
            new_img_rel = current["prod_img"]

    new_vals = {
        "img": (new_img_rel if imageFile is not None else current["prod_img"]),
        "name": (prod_name.strip() if prod_name is not None else current["prod_name"]),
        "price": (prod_price if prod_price is not None else current["prod_price"]),
        "cid": (category_id if category_id is not None else current["category_id"]),
        "pid": prod_id,
    }

    row = db.execute(
        text("""
            UPDATE product
               SET prod_img = :img,
                   prod_name = :name,
                   prod_price = :price,
                   category_id = :cid
             WHERE prod_id = :pid
         RETURNING prod_id, prod_img, prod_name, prod_price, category_id, is_active
        """),
        new_vals,
    ).mappings().first()
    db.commit()

    cat = db.execute(
        text("SELECT category_name FROM product_categories WHERE category_id = :cid"),
        {"cid": row["category_id"]},
    ).mappings().first()

    result = dict(row)
    result["category_name"] = cat["category_name"] if cat else None
    return result

# ====== LIST BY CATEGORY / SEARCH / TOGGLE ACTIVE (เหมือนเดิม)… ======
@router.get("/by-category/{category_id}", response_model=List[ProductOut])
def list_products_by_category(category_id: int, db: Session = Depends(get_db)):
    sql = """
        SELECT
            p.prod_id,
            p.prod_img,
            p.prod_name,
            p.prod_price,
            p.category_id,
            p.is_active,
            c.category_name
        FROM product p
        LEFT JOIN product_categories c ON c.category_id = p.category_id
        WHERE p.category_id = :cid
        ORDER BY p.prod_name ASC, p.prod_id ASC
    """
    rows = db.execute(text(sql), {"cid": category_id}).mappings().all()
    return rows

@router.get("/search", response_model=List[ProductOut])
def search_products_by_name(
    q: str = Query(..., min_length=1, description="คำค้นหาชื่อสินค้า (บางส่วนได้)"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    sql = """
      SELECT
        p.prod_id,
        p.prod_img,
        p.prod_name,
        p.prod_price,
        p.category_id,
        p.is_active,
        c.category_name
      FROM product p
      LEFT JOIN product_categories c
        ON c.category_id = p.category_id
      WHERE p.prod_name ILIKE :q
      ORDER BY p.prod_name ASC, p.prod_id ASC
      LIMIT :limit OFFSET :offset
    """
    rows = db.execute(text(sql), {"q": f"%{q.strip()}%", "limit": limit, "offset": offset}).mappings().all()
    return rows

@router.put("/{prod_id}/active", response_model=ProductOut)
def toggle_product_active(prod_id: int, is_active: bool, db: Session = Depends(get_db)):
    current = db.execute(text("SELECT * FROM product WHERE prod_id = :pid"), {"pid": prod_id}).mappings().first()
    if not current:
        raise HTTPException(status_code=404, detail="Product not found")

    row = db.execute(
        text("""
            UPDATE product
               SET is_active = :active
             WHERE prod_id = :pid
         RETURNING prod_id, prod_img, prod_name, prod_price, category_id, is_active
        """),
        {"pid": prod_id, "active": is_active},
    ).mappings().first()
    db.commit()

    cat = db.execute(
        text("SELECT category_name FROM product_categories WHERE category_id = :cid"),
        {"cid": row["category_id"]},
    ).mappings().first()

    result = dict(row)
    result["category_name"] = cat["category_name"] if cat else None
    return result
