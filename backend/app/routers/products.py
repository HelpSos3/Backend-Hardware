# backend/app/routers/products.py
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from fastapi.responses import Response
from pydantic import BaseModel
from decimal import Decimal
from typing import Optional, List
from sqlalchemy.orm import Session
from sqlalchemy import text
import os, uuid, shutil
from typing import Optional, List 
from fastapi import Query
from pydantic import BaseModel
from app.database import SessionLocal

router = APIRouter(prefix="/products", tags=["products"])

# ====== CONFIG & HELPERS ======
UPLOAD_DIR = "/app/app/uploads/products"  # ตรงกับ main.py ที่ mount /uploads -> app/uploads
os.makedirs(UPLOAD_DIR, exist_ok=True)

# root ของ uploads เพื่อประกอบ path เวลาลบไฟล์เก่า
UPLOAD_ROOT = os.path.dirname(UPLOAD_DIR)  # "/app/app/uploads"

ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".webp"}

def save_image(file: UploadFile | None) -> Optional[str]:
    """บันทึกรูปภาพลงดิสก์และคืนค่า relative path เช่น 'products/xxxx.jpg'"""
    if not file:
        return None
    # ตรวจนามสกุล
    _, ext = os.path.splitext(file.filename or "")
    ext = ext.lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(status_code=400, detail="รองรับไฟล์เฉพาะ .jpg .jpeg .png .webp")

    fname = f"{uuid.uuid4().hex}{ext}"
    abs_path = os.path.join(UPLOAD_DIR, fname)
    # เขียนไฟล์
    with open(abs_path, "wb") as out:
        shutil.copyfileobj(file.file, out)

    # relative path (ไว้ให้ frontend ประกอบเป็น /uploads/products/<fname>)
    return f"products/{fname}"

def delete_image(rel_path: Optional[str]) -> None:
    """ลบไฟล์รูปจากดิสก์ ถ้ามี เช่น rel_path='products/xxx.jpg'"""
    if not rel_path:
        return
    safe_rel = rel_path.replace("..", "").lstrip("/\\")
    abs_path = os.path.join(UPLOAD_ROOT, safe_rel)
    try:
        if os.path.isfile(abs_path):
            os.remove(abs_path)
    except Exception:
        # อย่าทำให้ API พังเพราะลบรูปไม่สำเร็จ
        pass

# ====== DB session ======
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ====== Schemas (สำหรับ response / JSON-only case) ======
class ProductOut(BaseModel):
    prod_id: int
    prod_img: Optional[str] = None
    prod_name: str
    prod_price: Decimal
    category_id: int
    category_name: Optional[str] = None

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
    label: str   # ชื่อที่โชว์    

# ====== LIST ======
@router.get("/", response_model=List[ProductOut])
def list_products(db: Session = Depends(get_db)):
    sql = """
    SELECT
      p.prod_id,
      p.prod_img,
      p.prod_name,
      p.prod_price,
      p.category_id,
      c.category_name
    FROM product p
    LEFT JOIN product_categories c
      ON c.category_id = p.category_id
    ORDER BY p.prod_id ASC
    """
    rows = db.execute(text(sql)).mappings().all()
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
    # ตรวจว่าหมวดหมู่มีจริง
    category = db.execute(
        text("SELECT category_id, category_name FROM product_categories WHERE category_id = :cid"),
        {"cid": category_id},
    ).mappings().first()
    if not category:
        raise HTTPException(status_code=400, detail="Category not found")

    # บันทึกรูปถ้ามี
    img_rel = save_image(imageFile)

    # Insert
    row = db.execute(
        text("""
            INSERT INTO product (prod_img, prod_name, prod_price, category_id)
            VALUES (:img, :name, :price, :cid)
            RETURNING prod_id, prod_img, prod_name, prod_price, category_id
        """),
        {
            "img": img_rel,
            "name": prod_name.strip(),
            "price": prod_price,
            "cid": category_id,
        },
    ).mappings().first()
    db.commit()

    result = dict(row)
    result["category_name"] = category["category_name"]
    return result

# ====== UPDATE (PUT แบบ multipart/form-data เหมือน POST) ======
@router.put("/{prod_id}", response_model=ProductOut)
async def update_product(
    prod_id: int,
    prod_name: str = Form(...),
    prod_price: Decimal = Form(...),
    category_id: int = Form(...),
    imageFile: UploadFile | None = File(None),
    db: Session = Depends(get_db),
):
    # มีสินค้านี้ไหม
    current = db.execute(
        text("SELECT * FROM product WHERE prod_id = :pid"),
        {"pid": prod_id},
    ).mappings().first()
    if not current:
        raise HTTPException(status_code=404, detail="Product not found")

    # ตรวจหมวดหมู่มีจริง
    cat = db.execute(
        text("SELECT category_id, category_name FROM product_categories WHERE category_id = :cid"),
        {"cid": category_id},
    ).mappings().first()
    if not cat:
        raise HTTPException(status_code=400, detail="Category not found")

    # ถ้ามีรูปใหม่ -> เซฟใหม่และลบรูปเก่า
    new_img_rel = current["prod_img"]
    if imageFile is not None and imageFile.filename:
        new_img_rel = save_image(imageFile)
        # ลบไฟล์เก่า (ถ้ามี)
        try:
            delete_image(current["prod_img"])
        except Exception:
            pass

    # อัปเดตข้อมูล
    row = db.execute(
        text("""
            UPDATE product
               SET prod_img = :img,
                   prod_name = :name,
                   prod_price = :price,
                   category_id = :cid
             WHERE prod_id = :pid
         RETURNING prod_id, prod_img, prod_name, prod_price, category_id
        """),
        {
            "img": new_img_rel,
            "name": prod_name.strip(),
            "price": prod_price,
            "cid": category_id,
            "pid": prod_id,
        },
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
    # เช็คสินค้าว่ามีอยู่จริง
    current = db.execute(
        text("SELECT * FROM product WHERE prod_id = :pid"),
        {"pid": prod_id},
    ).mappings().first()
    if not current:
        raise HTTPException(status_code=404, detail="Product not found")

    # ถ้าขอเปลี่ยนหมวด ตรวจว่ามีจริง
    if category_id is not None:
        cat = db.execute(
            text("SELECT category_id FROM product_categories WHERE category_id = :cid"),
            {"cid": category_id},
        ).mappings().first()
        if not cat:
            raise HTTPException(status_code=400, detail="Category not found")

    # จัดการรูปภาพ (ถ้ามีไฟล์ใหม่)
    new_img_rel = None
    if imageFile is not None:
        # ถ้า filename มีค่า แปลว่าต้องการอัปเดตรูป
        if imageFile.filename:
            new_img_rel = save_image(imageFile)
            # ลบรูปเก่าถ้ามี
            delete_image(current["prod_img"])
        else:
            # ส่งไฟล์ว่าง -> ไม่เปลี่ยนรูป
            new_img_rel = current["prod_img"]

    # รวมค่าใหม่
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
         RETURNING prod_id, prod_img, prod_name, prod_price, category_id
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

# ====== DELETE ======
@router.delete("/{prod_id}", status_code=204)
def delete_product(prod_id: int, db: Session = Depends(get_db)):
    # ลบรูปเก่าก่อนลบแถว
    current = db.execute(
        text("SELECT prod_img FROM product WHERE prod_id = :pid"),
        {"pid": prod_id},
    ).mappings().first()

    res = db.execute(
        text("DELETE FROM product WHERE prod_id = :pid"),
        {"pid": prod_id},
    )
    db.commit()
    if res.rowcount == 0:
        raise HTTPException(status_code=404, detail="Product not found")

    # ลบไฟล์รูปในดิสก์ (ถ้ามี)
    if current and current["prod_img"]:
        delete_image(current["prod_img"])

    return Response(status_code=204)

@router.get("/by-category/{category_id}", response_model=List[ProductOut])
def list_products_by_category(category_id: int, db: Session = Depends(get_db)):
    sql = """
      SELECT p.prod_id,
             p.prod_img,
             p.prod_name,
             p.prod_price,
             p.category_id,
             c.category_name
      FROM product p
      LEFT JOIN product_categories c
        ON c.category_id = p.category_id
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
        c.category_name
      FROM product p
      LEFT JOIN product_categories c
        ON c.category_id = p.category_id
      WHERE p.prod_name ILIKE :q
      ORDER BY p.prod_name ASC, p.prod_id ASC
      LIMIT :limit OFFSET :offset
    """
    rows = db.execute(
        text(sql),
        {"q": f"%{q.strip()}%", "limit": limit, "offset": offset},
    ).mappings().all()
    return rows