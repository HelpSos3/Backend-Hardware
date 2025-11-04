from fastapi import FastAPI
from .routers import idcard , camera , printer, scale


app = FastAPI(title="Hardware API (Thai ID card)")

# เส้นทางสำหรับสแกนบัตร
app.include_router(idcard.router, prefix="/idcard", tags=["idcard"])
app.include_router(camera.router)
app.include_router(printer.router)
app.include_router(scale.router)


@app.get("/")
def health():
    return {"status": "ok", "service": "hardware-service"}
