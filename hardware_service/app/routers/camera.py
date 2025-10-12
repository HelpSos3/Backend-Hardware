# hardware_service/app/routers/camera.py
from fastapi import APIRouter, Response, HTTPException, Query
from fastapi.responses import StreamingResponse, PlainTextResponse
import cv2, time, numpy as np, os, platform
from typing import Generator, Optional, List, Tuple

router = APIRouter(prefix="/camera", tags=["camera"])

# ===== Config =====
SCAN_MAX = int(os.getenv("CAMERA_SCAN_MAX", "6"))     # สแกน index 0..SCAN_MAX
DEFAULT_WARMUP = int(os.getenv("CAMERA_WARMUP", "8"))
DEFAULT_FPS = int(os.getenv("CAMERA_FPS", "15"))

def _backends_for_platform() -> Tuple[int, ...]:
    sysname = platform.system().lower()
    if "windows" in sysname:
        return (cv2.CAP_MSMF, cv2.CAP_DSHOW, 0)
    if "linux" in sysname:
        return (cv2.CAP_V4L2 if hasattr(cv2, "CAP_V4L2") else 0, 0)
    if "darwin" in sysname or "mac" in sysname:
        return (cv2.CAP_AVFOUNDATION if hasattr(cv2, "CAP_AVFOUNDATION") else 0, 0)
    return (0,)

def _try_open(idx: int) -> Optional[cv2.VideoCapture]:
    for backend in _backends_for_platform():
        cap = cv2.VideoCapture(idx, backend)
        if cap is not None and cap.isOpened():
            return cap
        try:
            cap.release()
        except Exception:
            pass
    return None

def _open_cam(device_index: int) -> Optional[cv2.VideoCapture]:
    # device_index >= 0 = บังคับใช้ index นั้น
    if device_index >= 0:
        return _try_open(device_index)
    # device_index == -1 = auto-scan
    for idx in range(SCAN_MAX + 1):
        cap = _try_open(idx)
        if cap is not None:
            return cap
    return None

def _configure_cap(cap: cv2.VideoCapture, width: int, height: int, fps: int):
    cap.set(cv2.CAP_PROP_FRAME_WIDTH,  float(width))
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, float(height))
    try: cap.set(cv2.CAP_PROP_FPS, float(fps))
    except Exception: pass
    # best-effort ไม่พังถ้าไม่รองรับ
    try: cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 0.75)
    except Exception: pass
    try: cap.set(cv2.CAP_PROP_AUTOFOCUS, 1)
    except Exception: pass

def _warmup(cap: cv2.VideoCapture, warmup: int):
    for _ in range(max(0, warmup)):
        cap.read()
        time.sleep(0.06)

def _black_jpeg(w: int, h: int) -> bytes:
    black = np.zeros((int(h), int(w), 3), dtype=np.uint8)
    ok, buf = cv2.imencode(".jpg", black)
    return buf.tobytes() if ok else b""

def _first_working_index(width: int, height: int, fps: int, warmup: int) -> Optional[int]:
    for idx in range(SCAN_MAX + 1):
        cap = _try_open(idx)
        if cap is None:
            continue
        try:
            _configure_cap(cap, width, height, fps)
            _warmup(cap, warmup)
            ok, frame = cap.read()
            if ok and frame is not None and frame.size > 0:
                if np.mean(frame) > 1.0:
                    return idx
        finally:
            try: cap.release()
            except Exception: pass
    return None

@router.get("/ping", response_class=PlainTextResponse)
def ping():
    return "ok"

@router.get("/devices")
def devices(width: int = Query(640, ge=160, le=3840),
            height: int = Query(360, ge=120, le=2160),
            warmup: int = Query(2, ge=0, le=30)):
    """
    สแกน index 0..SCAN_MAX แล้วคืนเฉพาะตัวที่เปิดได้และอ่านเฟรมได้
    หมายเหตุ: OpenCV ข้ามชื่ออุปกรณ์ไม่ได้ คืนได้แค่ index และ meta คร่าวๆ
    """
    found: List[dict] = []
    for idx in range(SCAN_MAX + 1):
        cap = _try_open(idx)
        if cap is None: 
            continue
        try:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH,  float(width))
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, float(height))
            _warmup(cap, warmup)
            ok, frame = cap.read()
            if ok and frame is not None and frame.size > 0:
                h, w = frame.shape[:2]
                found.append({
                    "index": idx,
                    "width": int(w),
                    "height": int(h),
                    "fps": float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
                })
        finally:
            try: cap.release()
            except Exception: pass
    return {"devices": found, "scan_max": SCAN_MAX}

@router.get("/auto-index")
def auto_index(width: int = Query(1280, ge=160, le=3840),
               height: int = Query(720, ge=120, le=2160),
               fps: int = Query(DEFAULT_FPS, ge=1, le=60),
               warmup: int = Query(DEFAULT_WARMUP, ge=0, le=30)):
    idx = _first_working_index(width, height, fps, warmup)
    if idx is None:
        raise HTTPException(status_code=404, detail="No working camera found")
    return {"index": idx}

@router.post("/capture", response_class=Response)
def capture(
    device_index: int = Query(0, description=">=0 = ใช้กล้องนั้น, -1 = auto"),
    width: int = Query(1280, ge=160, le=3840),
    height: int = Query(720, ge=120, le=2160),
    warmup: int = Query(DEFAULT_WARMUP, ge=0, le=30),
    fps: int = Query(DEFAULT_FPS, ge=1, le=60),
):
    cap = _open_cam(device_index)
    if cap is None:
        raise HTTPException(status_code=500, detail=f"Cannot open camera (index={device_index})")

    _configure_cap(cap, width, height, fps)
    _warmup(cap, warmup)

    ok, frame = cap.read()
    cap.release()
    if not ok or frame is None or frame.size == 0:
        raise HTTPException(status_code=500, detail="Capture failed")
    if np.mean(frame) < 2:
        raise HTTPException(status_code=503, detail="Frame too dark; increase warmup or lighting")

    ok, buf = cv2.imencode(".jpg", frame)
    if not ok:
        raise HTTPException(status_code=500, detail="JPEG encode failed")
    return Response(content=buf.tobytes(), media_type="image/jpeg", headers={"Cache-Control": "no-store"})

@router.get("/preview")
def preview(
    device_index: int = Query(0, description=">=0 = ใช้กล้องนั้น, -1 = auto"),
    width: int = Query(1280, ge=160, le=3840),
    height: int = Query(720, ge=120, le=2160),
    warmup: int = Query(DEFAULT_WARMUP, ge=0, le=60),
    fps: int = Query(DEFAULT_FPS, ge=1, le=60),
    heartbeat_sec: float = Query(0.0, ge=0.0, le=10.0),
):
    cap = _open_cam(device_index)
    if cap is None:
        raise HTTPException(status_code=500, detail=f"Cannot open camera (index={device_index})")

    _configure_cap(cap, width, height, fps)
    _warmup(cap, warmup)

    delay = 1.0 / float(fps)
    last_sent = time.time()

    def gen() -> Generator[bytes, None, None]:
        nonlocal last_sent
        try:
            while True:
                ok, frame = cap.read()
                if not ok or frame is None or frame.size == 0:
                    chunk = _black_jpeg(width, height)
                else:
                    if np.mean(frame) < 2:
                        frame[:] = (0, 0, 0)
                    ok2, buf2 = cv2.imencode(".jpg", frame)
                    chunk = buf2.tobytes() if ok2 else _black_jpeg(width, height)

                yield (b"--frame\r\n"
                       b"Content-Type: image/jpeg\r\n\r\n" + chunk + b"\r\n")
                last_sent = time.time()
                time.sleep(delay)

                if heartbeat_sec > 0 and (time.time() - last_sent) > heartbeat_sec:
                    hb = _black_jpeg(width, height)
                    yield (b"--frame\r\n"
                           b"Content-Type: image/jpeg\r\n\r\n" + hb + b"\r\n")
                    last_sent = time.time()
        except GeneratorExit:
            pass
        finally:
            try:
                cap.release()
            except Exception:
                pass

    return StreamingResponse(
        gen(),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"}
    )

@router.get("/status")
def camera_status(device_index: int = Query(0, description=">=0 = ใช้กล้องนั้น, -1 = auto"),
                  width: int = Query(640, ge=160, le=3840),
                  height: int = Query(360, ge=120, le=2160),
                  fps: int = Query(DEFAULT_FPS, ge=1, le=60),
                  warmup: int = Query(2, ge=0, le=30)):
    cap = _open_cam(device_index)
    if cap is None:
        return {"status": "disconnected", "message": f"เปิดไม่ได้ index={device_index}"}
    try:
        _configure_cap(cap, width, height, fps)
        _warmup(cap, warmup)
        ok, frame = cap.read()
        if not ok or frame is None or frame.size == 0:
            return {"status": "error", "message": "เปิดกล้องได้ แต่อ่านภาพไม่ได้"}
        h, w = frame.shape[:2]
        return {
            "status": "connected",
            "message": "กล้องพร้อมใช้งาน",
            "meta": {
                "width": int(w), "height": int(h),
                "fps": float(cap.get(cv2.CAP_PROP_FPS) or 0.0),
                "brightness": round(float(np.mean(frame)), 2),
            }
        }
    finally:
        try: cap.release()
        except Exception: pass
