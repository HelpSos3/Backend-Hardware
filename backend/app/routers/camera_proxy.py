# backend/app/routers/camera_proxy.py
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse , StreamingResponse
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
    """ตรวจสอบว่า hardware_service ตอบหรือไม่"""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(3.0)) as client:
            r = await client.get(_hw("/healthz"))
            ok = (r.status_code == 200)
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


@router.get("/live")
async def camera_live():
    """
    Proxy สตรีมสดจาก hardware_service /camera/live (ไม่ใช้ token)
    - ตาม redirect ไป /camera/preview?...
    - ส่ง multipart/x-mixed-replace กลับให้ client
    """
    url = _hw("/camera/live")
    try:
        async with httpx.AsyncClient(timeout=None, follow_redirects=True) as client:
            async with client.stream("GET", url) as resp:
                resp.raise_for_status()
                content_type = resp.headers.get(
                    "content-type",
                    "multipart/x-mixed-replace; boundary=frame"
                )

                async def _iter():
                    async for chunk in resp.aiter_bytes():
                        yield chunk

                return StreamingResponse(
                    _iter(),
                    media_type=content_type,
                    headers={
                        "Cache-Control": "no-store",
                        "X-Accel-Buffering": "no",
                    },
                )
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"hardware live error: {e}")