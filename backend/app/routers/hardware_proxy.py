# backend/app/routers/hardware_proxy.py
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
import os, requests

router = APIRouter(prefix="/hardware", tags=["hardware"])

HARDWARE_URL = os.getenv("HARDWARE_URL", "http://host.docker.internal:9000")

@router.get("/camera/status")
def camera_status(
    device_index: int = Query(0, description="index กล้อง 0,1,..."),
    probe_frame: bool = Query(True),
    timeout_sec: int = Query(5, ge=1, le=30)
):
    try:
        r = requests.get(
            f"{HARDWARE_URL}/camera/status",
            params={"device_index": device_index, "probe_frame": probe_frame},
            timeout=timeout_sec,
        )
        r.raise_for_status()
    except requests.RequestException as e:
        # ถึง hardware ไม่ได้เลย
        raise HTTPException(status_code=502, detail=f"Hardware unreachable: {e}")

    data = r.json()
    # บังคับให้มีฟิลด์หลักเสมอ
    return JSONResponse({
        "connected": bool(data.get("connected")),
        "device_index": device_index,
        "can_read_frame": bool(data.get("can_read_frame", False)),
        "width": data.get("width"),
        "height": data.get("height"),
        "message": data.get("message"),
        "tip": data.get("tip"),
    })
