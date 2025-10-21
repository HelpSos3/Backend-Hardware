# backend/app/routers/camera_proxy.py
from fastapi import APIRouter, HTTPException , Query
from fastapi.responses import JSONResponse   , HTMLResponse , StreamingResponse
import os , httpx

router = APIRouter(prefix="/camera", tags=["camera"])

# ========== CONFIG ==========
HARDWARE_URL = os.getenv("HARDWARE_URL", "http://host.docker.internal:9000")

DEFAULT_FPS = int(os.getenv("CAMERA_FPS", "15"))

# ========== Helpers ==========
def _hw(path: str) -> str:
    return f"{HARDWARE_URL.rstrip('/')}{path}"



# ========== Health ==========
@router.get("/health")
async def health():
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(3.0)) as client:
            r = await client.get(_hw("/camera/ping"))
            ok = (r.status_code == 200 and r.text.strip() == "ok")
    except httpx.HTTPError:
        ok = False
    return {"ok": ok, "hardware_url": HARDWARE_URL}

# ========== Devices ==========
@router.get("/devices")
async def hardware_camera_devices():
    """
    ดึงรายการกล้องทั้งหมดจาก hardware_service (/camera/devices)
    """
    url = f"{HARDWARE_URL.rstrip('/')}/camera/devices"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            r = await client.get(url)
            r.raise_for_status()
            return JSONResponse(r.json())
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"hardware error: {e}")

