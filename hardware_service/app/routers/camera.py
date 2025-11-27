# hardware_service/app/routers/camera.py
from fastapi import APIRouter, Response, HTTPException, Query
from fastapi.responses import StreamingResponse, PlainTextResponse
import cv2
import time
import os
import numpy as np
from typing import Optional

router = APIRouter(prefix="/camera", tags=["camera"])

# ===== Config =====
SCAN_MAX = int(os.getenv("CAMERA_SCAN_MAX", "6"))
DEFAULT_WARMUP = int(os.getenv("CAMERA_WARMUP", "8"))
DEFAULT_FPS = int(os.getenv("CAMERA_FPS", "15"))
DEFAULT_JPEG_QUALITY = int(os.getenv("CAMERA_JPEG_QUALITY", "85"))

# ===== Helpers (Windows-only backends) =====
def _backend_from_str(name: str) -> int:
    n = (name or "auto").lower()
    if n == "dshow":
        return getattr(cv2, "CAP_DSHOW", 0)
    if n == "msmf":
        return getattr(cv2, "CAP_MSMF", 0)
    return 0  # auto

def _try_open(idx: int) -> Optional[cv2.VideoCapture]:
    # ลองเปิดเฉพาะ backend ของ Windows: DirectShow → Media Foundation
    for backend in (cv2.CAP_DSHOW, cv2.CAP_MSMF):
        cap = cv2.VideoCapture(idx, backend)
        if cap is not None and cap.isOpened():
            return cap
        try:
            cap.release()
        except Exception:
            pass
    return None

def _open_cam_with_backend(device_index: int, backend_name: str) -> Optional[cv2.VideoCapture]:
    if device_index < 0 or device_index > SCAN_MAX:
        return None
    be = _backend_from_str(backend_name)
    if be == 0:
        return _try_open(device_index)  # auto
    cap = cv2.VideoCapture(device_index, be)
    if cap is not None and cap.isOpened():
        return cap
    try:
        cap.release()
    except Exception:
        pass
    return None

def _set_if_supported(cap: cv2.VideoCapture, prop: int, value: float):
    try:
        cap.set(prop, value)
    except Exception:
        pass

def _apply_fourcc(cap: cv2.VideoCapture, codec: str):
    up = (codec or "AUTO").upper()
    if up in ("MJPG", "YUY2"):
        try:
            fourcc = cv2.VideoWriter_fourcc(*up)
            cap.set(cv2.CAP_PROP_FOURCC, float(fourcc))
        except Exception:
            pass

def _configure_cap_standard(cap, width, height, fps, codec="AUTO"):
    _set_if_supported(cap, cv2.CAP_PROP_FRAME_WIDTH,  float(width))
    _set_if_supported(cap, cv2.CAP_PROP_FRAME_HEIGHT, float(height))
    _set_if_supported(cap, cv2.CAP_PROP_FPS,          float(fps))
    if hasattr(cv2, "CAP_PROP_BUFFERSIZE"):
        _set_if_supported(cap, cv2.CAP_PROP_BUFFERSIZE, 1)

def _configure_cap_quick(cap, width, height, codec_hint="AUTO", set_fps=None):
    try:
        cap.set(cv2.CAP_PROP_FRAME_WIDTH,  float(width))
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, float(height))
    except Exception:
        pass
    if hasattr(cv2, "CAP_PROP_BUFFERSIZE"):
        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass
    if set_fps is not None:
        try:
            cap.set(cv2.CAP_PROP_FPS, float(set_fps))
        except Exception:
            pass

def _should_quick_path(backend, codec, fps, fps_strategy):
    s  = (fps_strategy or "auto").lower()
    be = (backend or "auto").lower()
    co = (codec   or "AUTO").upper()
    if s == "skip":
        return True, None
    if s == "force":
        return False, float(fps)
    if be == "dshow" and co == "YUY2" and float(fps) >= 50.0:
        return True, None
    return False, float(fps)

def _warmup(cap, warmup):
    for _ in range(max(0, warmup)):
        cap.read()
        time.sleep(0.06)

def _black_jpeg(w, h, quality=DEFAULT_JPEG_QUALITY):
    black = np.zeros((int(h), int(w), 3), dtype=np.uint8)
    ok, buf = cv2.imencode(".jpg", black, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
    return buf.tobytes() if ok else b""

def _encode_jpeg(frame, quality=DEFAULT_JPEG_QUALITY):
    ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
    return buf.tobytes() if ok else b""

def _backend_name(be: int) -> str:
    return (
        "dshow" if be == getattr(cv2, "CAP_DSHOW", -1) else
        "msmf"  if be == getattr(cv2, "CAP_MSMF", -1) else
        "auto"  if be == 0 else str(be)
    )

# ===== Routes =====

# --- Capture ---
@router.post("/capture", response_class=Response)
def capture(
    device_index: int = Query(0, ge=0, le=SCAN_MAX),
    width: int = Query(1280, ge=1),
    height: int = Query(720, ge=1),
    warmup: int = Query(3, ge=0),
    fps: int = Query(15, ge=1),
    jpeg_quality: int = Query(DEFAULT_JPEG_QUALITY, ge=1, le=100),
    backend: str = Query("msmf"),
    codec: str = Query("MJPG"),
    fps_strategy: str = Query("auto")
):
    cap = _open_cam_with_backend(device_index, backend)
    if cap is None:
        raise HTTPException(status_code=400, detail=f"Invalid camera index/backend: idx={device_index}, backend={backend}")
    try:
        _apply_fourcc(cap, codec)
        use_quick, set_fps_value = _should_quick_path(backend, codec, fps, fps_strategy)
        if use_quick:
            _configure_cap_quick(cap, width, height, codec_hint=codec, set_fps=set_fps_value)
        else:
            _configure_cap_standard(cap, width, height, set_fps_value if set_fps_value else fps, codec=codec)
        _warmup(cap, warmup)
        ok, frame = cap.read()
    finally:
        cap.release()
    if not ok or frame is None or frame.size == 0:
        raise HTTPException(status_code=500, detail="Capture failed")
    data = _encode_jpeg(frame, quality=jpeg_quality)
    return Response(content=data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

# --- Preview (MJPEG stream) ---
@router.get("/preview")
def preview(
    device_index: int = Query(0, ge=0, le=SCAN_MAX),
    width: int = Query(1280, ge=1),
    height: int = Query(720, ge=1),
    warmup: int = Query(3, ge=0),
    fps: int = Query(15, ge=1),
    jpeg_quality: int = Query(DEFAULT_JPEG_QUALITY, ge=1, le=100),
    backend: str = Query("msmf"),
    codec: str = Query("MJPG"),
    fps_strategy: str = Query("auto")
):
    cap = _open_cam_with_backend(device_index, backend)
    if cap is None:
        raise HTTPException(status_code=400, detail=f"Invalid camera index/backend: idx={device_index}, backend={backend}")
    _apply_fourcc(cap, codec)
    use_quick, set_fps_value = _should_quick_path(backend, codec, fps, fps_strategy)
    if use_quick:
        _configure_cap_quick(cap, width, height, codec_hint=codec, set_fps=set_fps_value)
    else:
        _configure_cap_standard(cap, width, height, set_fps_value if set_fps_value else fps, codec=codec)
    _warmup(cap, warmup)

    delay = max(1.0 / float(fps), 0.001)
    def gen():
        try:
            while True:
                ok, frame = cap.read()
                if not ok or frame is None:
                    chunk = _black_jpeg(width, height, jpeg_quality)
                else:
                    chunk = _encode_jpeg(frame, jpeg_quality)
                yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + chunk + b"\r\n"
                time.sleep(delay)
        except GeneratorExit:
            pass
        finally:
            cap.release()

    return StreamingResponse(
        gen(),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"}
    )


