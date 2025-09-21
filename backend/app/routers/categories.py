from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import List
from sqlalchemy.orm import Session
from sqlalchemy import text
from app.database import SessionLocal

router = APIRouter(prefix="/categories", tags=["categories"])

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

class CategoryOut(BaseModel):
    category_id: int
    category_name: str

class CategoryCreate(BaseModel):
    category_name: str

# dropdown ของหมวด (option)
class CategoryOption(BaseModel):
    value: int
    label: str

@router.get("/", response_model=List[CategoryOut])
def list_categories(db: Session = Depends(get_db)):
    rows = db.execute(
        text("SELECT category_id, category_name FROM product_categories ORDER BY category_name ASC")
    ).mappings().all()
    return rows

@router.post("/", response_model=CategoryOut, status_code=201)
def create_category(body: CategoryCreate, db: Session = Depends(get_db)):
    name = body.category_name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="category_name is required")

    exists = db.execute(
        text("SELECT 1 FROM product_categories WHERE LOWER(category_name)=LOWER(:name)"),
        {"name": name},
    ).mappings().first()
    if exists:
        raise HTTPException(status_code=409, detail="Category name already exists")

    row = db.execute(
        text("""
            INSERT INTO product_categories (category_name)
            VALUES (:name)
            RETURNING category_id, category_name
        """),
        {"name": name},
    ).mappings().first()
    db.commit()
    return row


