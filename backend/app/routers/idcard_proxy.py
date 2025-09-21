# backend/app/routers/idcard_proxy.py
from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import text
from ..database import engine
import requests, os, base64, datetime

router = APIRouter(prefix="/idcard", tags=["idcard"])

HARDWARE_URL = os.getenv("HARDWARE_URL", "http://host.docker.internal:9000")

@router.post("/scan-and-save")
def scan_and_save(reader_index: int = Query(0, ge=0), with_photo: int = Query(1)):
    try:
        # 1) เรียก hardware_service
        r = requests.get(
            f"{HARDWARE_URL}/idcard/scan",
            params={"reader_index": reader_index, "with_photo": with_photo},
            timeout=30
        )
        if r.status_code != 200:
            raise HTTPException(status_code=502, detail=f"hardware error: {r.text}")
        data = r.json()

        with engine.begin() as conn:
            # 2) เช็คว่ามีลูกค้าอยู่แล้วหรือยัง
            existing = conn.execute(
                text("SELECT customer_id FROM customers WHERE national_id = :nid"),
                {"nid": data.get("national_id", "")}
            ).fetchone()

            if existing:
                return {
                    "ok": True,
                    "message": "ข้อมูลลูกค้ามีอยู่แล้ว",
                    "customer_id": existing[0]
                }

            # 3) ถ้าไม่มี ให้ insert ใหม่
            row = conn.execute(text("""
                INSERT INTO customers (full_name, national_id, address)
                VALUES (:full_name, :national_id, :address)
                RETURNING customer_id;
            """), {
                "full_name": data.get("full_name", ""),
                "national_id": data.get("national_id", ""),
                "address": data.get("address", "")
            }).fetchone()

            customer_id = row[0]

            saved_path = None
            if data.get("photo_base64"):
                ts = datetime.datetime.utcnow().strftime("%Y%m%d%H%M%S")
                filename = f"{data['national_id']}_{ts}.jpg"
                save_dir = "/app/app/uploads/idcard_photos"
                os.makedirs(save_dir, exist_ok=True)
                path = os.path.join(save_dir, filename)

                with open(path, "wb") as f:
                    f.write(base64.b64decode(data["photo_base64"]))

                conn.execute(text("""
                    INSERT INTO customer_photos (customer_id, photo_path)
                    VALUES (:cid, :path)
                """), {"cid": customer_id, "path": path})

                saved_path = path

        return {
            "ok": True,
            "message": "บันทึกลูกค้าใหม่เรียบร้อย",
            "customer_id": customer_id,
            "saved_photo": saved_path
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"scan-and-save error: {e}")
    


