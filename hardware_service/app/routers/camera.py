# hardware_service/app/routers/camera.py
from fastapi import APIRouter, Response, HTTPException, Query
from fastapi.responses import StreamingResponse
import cv2
import time
import os
import numpy as np
from typing import Optional
from pydantic import BaseModel

from app.services.camera_config import load_camera_config, save_camera_config

router = APIRouter(prefix="/camera", tags=["camera"])

# ===== Config =====
SCAN_MAX = int(os.getenv("CAMERA_SCAN_MAX", "6"))
DEFAULT_WARMUP = int(os.getenv("CAMERA_WARMUP", "3"))
DEFAULT_FPS = int(os.getenv("CAMERA_FPS", "15"))
DEFAULT_JPEG_QUALITY = int(os.getenv("CAMERA_JPEG_QUALITY", "85"))

# ===== Models =====
class CameraConfigRequest(BaseModel):
    active_camera: int | None = None


# ===== Helpers =====
def _try_open(idx: int) -> Optional[cv2.VideoCapture]:
    for backend in (cv2.CAP_DSHOW, cv2.CAP_MSMF):
        cap = cv2.VideoCapture(idx, backend)
        if cap and cap.isOpened():
            return cap
        try:
            cap.release()
        except Exception:
            pass
    return None


def _open_camera(idx: int) -> Optional[cv2.VideoCapture]:
    if idx < 0 or idx > SCAN_MAX:
        return None
    return _try_open(idx)


def _encode_jpeg(frame, quality=DEFAULT_JPEG_QUALITY) -> bytes:
    ok, buf = cv2.imencode(
        ".jpg",
        frame,
        [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)]
    )
    return buf.tobytes() if ok else b""


def _black_jpeg(w, h, quality=DEFAULT_JPEG_QUALITY) -> bytes:
    black = np.zeros((h, w, 3), dtype=np.uint8)
    return _encode_jpeg(black, quality)


def get_active_camera_index() -> int:
    cfg = load_camera_config()

    idx = cfg.get("active_camera")
    if idx is None:
        raise HTTPException(
            status_code=400,
            detail="Camera is not configured"
        )

    return idx



# ===== Routes =====

# --- List devices (for Settings page) ---
@router.get("/devices")
def list_camera_devices():
    devices = []

    for idx in range(SCAN_MAX + 1):
        cap = _try_open(idx)
        if cap:
            ok, _ = cap.read()
            cap.release()
            devices.append({
                "index": idx,
                "status": "available" if ok else "unavailable"
            })
        else:
            devices.append({
                "index": idx,
                "status": "unavailable"
            })

    return {"devices": devices}


# --- Get current config ---
@router.get("/config")
def get_camera_config():
    return load_camera_config()


# --- Set active camera ---
@router.post("/config")
def set_camera_config(req: CameraConfigRequest):
    cfg = load_camera_config()

    cfg["active_camera"] = req.active_camera

    save_camera_config(cfg)

    return {
        "message": "camera config saved",
        "config": cfg
    }


# --- Capture single image ---
@router.post("/capture", response_class=Response)
def capture(
    width: int = Query(1280, ge=1),
    height: int = Query(720, ge=1),
    warmup: int = Query(DEFAULT_WARMUP, ge=0),
    jpeg_quality: int = Query(DEFAULT_JPEG_QUALITY, ge=1, le=100),
):
    idx = get_active_camera_index()

    cap = _open_camera(idx)
    if cap is None:
        raise HTTPException(400, "Camera not available")

    try:
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)

        for _ in range(warmup):
            cap.read()
            time.sleep(0.05)

        ok, frame = cap.read()
    finally:
        cap.release()

    if not ok or frame is None:
        raise HTTPException(500, "Capture failed")

    data = _encode_jpeg(frame, jpeg_quality)
    return Response(
        content=data,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store"}
    )


# --- Preview (MJPEG stream) ---
@router.get("/preview")
def preview(
    width: int = Query(1280, ge=1),
    height: int = Query(720, ge=1),
    fps: int = Query(DEFAULT_FPS, ge=1),
    jpeg_quality: int = Query(DEFAULT_JPEG_QUALITY, ge=1, le=100),
):
    idx = get_active_camera_index()

    cap = _open_camera(idx)
    if cap is None:
        raise HTTPException(400, "Camera not available")

    cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)

    delay = max(1.0 / fps, 0.001)

    def gen():
        try:
            while True:
                ok, frame = cap.read()
                if not ok or frame is None:
                    chunk = _black_jpeg(width, height, jpeg_quality)
                else:
                    chunk = _encode_jpeg(frame, jpeg_quality)

                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n\r\n"
                    + chunk +
                    b"\r\n"
                )
                time.sleep(delay)
        finally:
            cap.release()

    return StreamingResponse(
        gen(),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-store"}
    )
