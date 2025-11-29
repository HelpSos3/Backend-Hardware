from fastapi import APIRouter , Depends ,Query
from sqlalchemy.orm  import Session
from sqlalchemy import text
from app.database import SessionLocal
from pydantic import BaseModel
from typing import List

router = APIRouter(prefix="/customers",tags=["customers"])

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

class CustomerList (BaseModel):
    customer_id: int
    full_name: str
    address: str
    photo: str| None
    last_purchase_date: str | None

class CustomerItem(BaseModel):
    customer_id: int
    full_name: str
    address: str
    national_id: str | None
    prod_name: str
    purchase_date: str
    purchase_time: str
    weight: float
    price: float
    payment_method: str
    category_name: str

class CustomerListResponse(BaseModel):
    items: List[CustomerList]
    total_items: int
    total_pages: int
    current_page: int
    per_page: int

class CustomerItemResponse(BaseModel):
    items: List[CustomerItem]
    total_items: int
    total_pages: int
    current_page: int
    per_page: int

@router.get("/", response_model=CustomerListResponse)
def list_customers(
    q:str | None = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1 , le=200),
    db: Session = Depends(get_db)
):
    offset = (page - 1) * per_page

    # จำนวนลูกค้าทั้งหมดที่มี
    count_sql = text("""
            SELECT COUNT(*)
            FROM customers c 
            WHERE (:q IS NULL OR c.full_name ILIKE '%' || :q || '%')

""")
    
    total_items = db.execute(count_sql, {"q": q}).scalar_one()
    sql = text("""
                SELECT
                    c.customer_id,
                    c.full_name,
                    c.address,
                    cp.photo_path AS photo,
                (
                    SELECT TO_CHAR(MAX(p.purchase_date), 'DD/MM/YYYY HH24:MI')
                    FROM purchases p
                    WHERE p.customer_id = c.customer_id
                ) AS last_purchase_date
            FROM customers c
            LEFT JOIN customer_photos cp ON cp.customer_id = c.customer_id
            WHERE (:q IS NULL OR c.full_name ILIKE '%' || :q || '%')
            ORDER BY 
                c.full_name NULLS LAST,
                c.customer_id ASC
            LIMIT :per_page
            OFFSET :offset;
""")
    rows = db.execute(sql,{"q":q, "per_page":per_page, "offset":offset}).mappings().all()

    total_pages = (total_items + per_page - 1) // per_page

    return CustomerListResponse(
    items=rows,
    total_items=total_items,
    total_pages=total_pages,
    current_page=page,
    per_page=per_page
)


@router.get("/{customer_id}",response_model=CustomerItemResponse)
def list_items(
    customer_id: int ,
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=200), 
    db:Session = Depends(get_db)):

    offset = (page - 1) * per_page

    count_sql = text("""
    SELECT COUNT(*)
    FROM purchases pu
    JOIN purchase_items pi ON pu.purchase_id = pi.purchase_id
    WHERE pu.customer_id = :customer_id
""")
    
    total_items = db.execute(count_sql,{"customer_id":customer_id}).scalar_one()
    sql = text("""
            SELECT
                c.customer_id,
                c.full_name,
                c.address,
                c.national_id,
                pr.prod_name,
                TO_CHAR(pi.purchase_items_date,'DD/MM/YYYY') AS purchase_date,
                TO_CHAR(pi.purchase_items_date,'HH24:MI') AS purchase_time,
                pi.weight,
                pi.price,
                pay.payment_method,
                cat.category_name 
            from purchases pu
            join customers c on pu.customer_id = c.customer_id
            join purchase_items pi on pu.purchase_id = pi.purchase_id
            join product pr on pr.prod_id = pi.prod_id
            join product_categories cat on cat.category_id = pr.category_id 	
            join payment pay on pay.purchase_id = pu.purchase_id
            where pu.customer_id = :customer_id
            ORDER BY pi.purchase_items_date DESC
            LIMIT :per_page
            OFFSET :offset   ;
""")
    rows = db.execute(sql,{"customer_id": customer_id,"per_page":per_page,"offset":offset}).mappings().all()

    total_pages =  (total_items + per_page -1) // per_page
    return CustomerItemResponse  (  
                        items=rows,
                        total_items=total_items,
                        total_pages=total_pages,
                        current_page=page,
                        per_page=per_page
                )