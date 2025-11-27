from fastapi import APIRouter , Depends ,Query
from sqlalchemy.orm  import Session
from sqlalchemy import text
from app.database import SessionLocal

router = APIRouter(prefix="/customers",tags=["customers"])

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@router.get("/")
def list_customers(
    q:str | None = None,
    db: Session = Depends(get_db)
):
    sql = text("""
            SELECT
               c.customer_id,
               c.full_name,
               c.address,
               cp.photo_path as photo,
               (
               SELECT MAX(purchase_date)
               FROM purchases p
               WHERE p.customer_id = c.customer_id
               ) AS last_purchase_date
            FROM customers c
            LEFT JOIN customer_photos cp on cp.customer_id = c.customer_id
            ORDER BY last_purchase_date DESC nulls LAST;
""")
    rows = db.execute(sql,{"q":q}).mappings().all()
    return rows

@router.get("/{customer_id}")
def list_items(customer_id: int , db:Session = Depends(get_db)):
    sql = text("""
            SELECT
                c.customer_id,
                c.full_name,
                c.address,
                c.national_id,
                pr.prod_name,
                TO_CHAR(pi.purchase_items_date,'DD/MM/YYYY') AS purchase_date,
                TO_CHAR(pi.purchase_items_date,'HH24:MI') AS purchase_Time,
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
            ORDER BY pu.purchase_date DESC;
""")
    rows = db.execute(sql,{"customer_id": customer_id}).mappings().all()
    return rows