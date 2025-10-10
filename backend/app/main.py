# backend/app/main.py
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import Response

from .models import create_tables
from .routers import products, categories
from .routers import purchases, purchase_items, payments
from .routers import hardware_proxy

app = FastAPI(title="Scrap Shop Backend")

# CORS: ระบุ origin ของ React ให้ชัด + เผื่อทั้ง localhost / 127.0.0.1
ALLOWED_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    # เผื่อกรณีพอร์ต dev อื่น ๆ เช่น 5173 ฯลฯ (ถ้าไม่ได้ใช้จะไม่แมตช์)
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_credentials=True,
    allow_methods=["*"],     # GET/POST/PUT/DELETE/OPTIONS ทั้งหมด
    allow_headers=["*"],     # อนุญาตทุก header รวมถึง Content-Type, Authorization
)

# เสิร์ฟไฟล์อัปโหลด: /uploads/** -> backend/app/uploads/**
app.mount("/uploads", StaticFiles(directory="app/uploads"), name="uploads")

# ให้ preflight (OPTIONS) ผ่านแน่นอน แม้ proxy/เบราว์เซอร์จะงอแง
@app.options("/{full_path:path}")
def preflight_handler(full_path: str) -> Response:
    return Response(status_code=204)

@app.on_event("startup")
def on_startup():
    create_tables()

app.include_router(hardware_proxy.router)


# include routers
app.include_router(categories.router)
app.include_router(products.router)

app.include_router(purchases.router)
app.include_router(purchase_items.router)
app.include_router(payments.router)




# health check
@app.get("/health")
def health():
    return {"status": "ok"}
