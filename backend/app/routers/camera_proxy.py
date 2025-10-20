# backend/app/routers/camera_proxy.py
from fastapi import APIRouter, HTTPException
from fastapi.responses import  JSONResponse
import os
import httpx
router = APIRouter(prefix="/camera", tags=["camera"])

# ========== CONFIG ==========
HARDWARE_URL = os.getenv("HARDWARE_URL", "http://host.docker.internal:9000")
DEFAULT_FPS = int(os.getenv("CAMERA_FPS", "15"))

# ========== Helpers ==========
def _hw(path: str) -> str:
    return f"{HARDWARE_URL.rstrip('/')}{path}"

# ========== Health (optional, เช็ค hardware) ==========
@router.get("/health")
async def health():
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(3.0)) as client:
            r = await client.get(_hw("/healthz"))
            ok = (r.status_code == 200)
    except httpx.HTTPError:
        ok = False
    return {"ok": ok, "hardware_url": HARDWARE_URL}

@router.get("/camera/devices")
async def hardware_camera_devices():
    url = f"{HARDWARE_URL.rstrip('/')}/camera/devices"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            r = await client.get(url)
            r.raise_for_status()
            return JSONResponse(r.json())
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"hardware error: {e}")



