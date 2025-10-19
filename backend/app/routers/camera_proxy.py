# backend/app/routers/camera_proxy.py
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse, JSONResponse, HTMLResponse, Response
import os
import httpx
from typing import AsyncGenerator

router = APIRouter(prefix="/camera", tags=["camera"])

# ========== CONFIG ==========
HARDWARE_URL = os.getenv("HARDWARE_URL", "http://host.docker.internal:9000")
DEFAULT_FPS = int(os.getenv("CAMERA_FPS", "15"))

# ========== Helpers ==========
def _hw(path: str) -> str:
    return f"{HARDWARE_URL.rstrip('/')}{path}"

def _mjpeg_headers() -> dict:
    return {
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
        "Content-Type": "multipart/x-mixed-replace; boundary=frame",
    }

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

# ========== Devices ==========
@router.get("/devices")
async def list_devices():
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            r = await client.get(_hw("/camera/devices"))
            r.raise_for_status()
        return JSONResponse(r.json())
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"hardware error: {e}")

# ========== Live Page (HTML) ==========
@router.get("/live_page", response_class=HTMLResponse)
async def live_page(
    device_index: int = Query(0, ge=0),
    fps: int = Query(DEFAULT_FPS, ge=1, le=60),
):
    """
    หน้า HTML สำหรับดูภาพสดผ่าน Backend (proxy ไป Hardware)
    """
    html = f"""
    <!doctype html>
    <html>
      <head>
        <meta charset="utf-8">
        <title>Camera Live Preview</title>
        <style>
          :root {{ color-scheme: dark; }}
          body {{ margin:0; background:#111; color:#eee;
                  font-family: system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif; }}
          .bar {{ padding:10px; font-size:14px; background:#181818; border-bottom:1px solid #222; }}
          img {{ display:block; margin:0 auto; max-width:100%; height:auto; }}
        </style>
      </head>
      <body>
        <div class="bar">Live Camera Preview | device_index={device_index} | fps={fps}</div>
        <img src="/camera/preview?device_index={device_index}&fps={fps}" alt="live"/>
      </body>
    </html>
    """
    return HTMLResponse(html)

# ========== Live Preview (MJPEG Stream) ==========
@router.get("/preview")
async def preview(
    device_index: int = Query(0, ge=0),
    fps: int = Query(DEFAULT_FPS, ge=1, le=60),
):
    """
    Proxy สตรีม MJPEG จาก Hardware → ให้ <img src="/camera/preview?..."> ใช้ได้เลย
    """
    async def stream() -> AsyncGenerator[bytes, None]:
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream(
                    "GET",
                    _hw("/camera/preview"),
                    params={"device_index": device_index, "fps": fps},
                ) as resp:
                    resp.raise_for_status()
                    async for chunk in resp.aiter_raw():
                        yield chunk
        except httpx.HTTPError:
            return  # upstream หลุดก็จบสตรีม

    return StreamingResponse(stream(), headers=_mjpeg_headers())




