# hardware_service/app/routers/camera.py
from fastapi import APIRouter, Response, HTTPException, Query, Depends, Header
from fastapi.responses import StreamingResponse, PlainTextResponse, HTMLResponse, JSONResponse, RedirectResponse
import cv2, time, numpy as np, os, platform
from time import perf_counter
from typing import Generator, Optional, Tuple

router = APIRouter(prefix="/camera", tags=["camera"])

# ===== Config =====
SCAN_MAX = int(os.getenv("CAMERA_SCAN_MAX", "6"))
DEFAULT_WARMUP = int(os.getenv("CAMERA_WARMUP", "8"))
DEFAULT_FPS = int(os.getenv("CAMERA_FPS", "15"))
DEFAULT_JPEG_QUALITY = int(os.getenv("CAMERA_JPEG_QUALITY", "85"))
DEFAULT_READ_TIMEOUT = float(os.getenv("CAMERA_READ_TIMEOUT", "1.0"))

# ===== Helpers =====
def _backends_for_platform() -> Tuple[int, ...]:
    sysname = platform.system().lower()
    if "windows" in sysname:
        return (getattr(cv2, "CAP_DSHOW", 0), getattr(cv2, "CAP_MSMF", 0), 0)
    if "linux" in sysname:
        return (getattr(cv2, "CAP_V4L2", 0), 0)
    if "darwin" in sysname or "mac" in sysname:
        return (getattr(cv2, "CAP_AVFOUNDATION", 0), 0)
    return (0,)

def _backend_from_str(name: str) -> int:
    name = (name or "auto").lower()
    if name == "dshow":
        return getattr(cv2, "CAP_DSHOW", 0)
    if name == "msmf":
        return getattr(cv2, "CAP_MSMF", 0)
    return 0

def _try_open(idx: int) -> Optional[cv2.VideoCapture]:
    for backend in _backends_for_platform():
        cap = cv2.VideoCapture(idx, backend)
        if cap is not None and cap.isOpened():
            return cap
        try: cap.release()
        except Exception: pass
    return None

def _open_cam_with_backend(device_index: int, backend_name: str) -> Optional[cv2.VideoCapture]:
    if device_index < 0 or device_index > SCAN_MAX:
        return None
    be = _backend_from_str(backend_name)
    if be == 0:
        return _try_open(device_index)
    cap = cv2.VideoCapture(device_index, be)
    if cap is not None and cap.isOpened():
        return cap
    try: cap.release()
    except Exception: pass
    return None

def _set_if_supported(cap: cv2.VideoCapture, prop: int, value: float):
    try: cap.set(prop, value)
    except Exception: pass

def _apply_fourcc(cap: cv2.VideoCapture, codec: str):
    up = (codec or "AUTO").upper()
    if up == "AUTO":
        return
    try:
        fourcc = cv2.VideoWriter_fourcc(*up)
        cap.set(cv2.CAP_PROP_FOURCC, float(fourcc))
    except Exception:
        pass

def _configure_cap_standard(cap, width, height, fps, codec="AUTO"):
    _set_if_supported(cap, cv2.CAP_PROP_FRAME_WIDTH,  float(width))
    _set_if_supported(cap, cv2.CAP_PROP_FRAME_HEIGHT, float(height))
    _set_if_supported(cap, cv2.CAP_PROP_FPS, float(fps))
    if hasattr(cv2, "CAP_PROP_BUFFERSIZE"):
        _set_if_supported(cap, cv2.CAP_PROP_BUFFERSIZE, 1)
    if codec.upper() == "MJPG":
        try:
            fourcc = cv2.VideoWriter_fourcc(*"MJPG")
            _set_if_supported(cap, cv2.CAP_PROP_FOURCC, float(fourcc))
        except Exception:
            pass
    _set_if_supported(cap, cv2.CAP_PROP_AUTO_EXPOSURE, 0.75)
    _set_if_supported(cap, cv2.CAP_PROP_AUTOFOCUS, 1)

def _configure_cap_quick(cap, width, height, codec_hint="AUTO", set_fps=None):
    try:
        cap.set(cv2.CAP_PROP_FRAME_WIDTH,  float(width))
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, float(height))
    except Exception:
        pass
    if hasattr(cv2, "CAP_PROP_BUFFERSIZE"):
        try: cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception: pass
    up = (codec_hint or "AUTO").upper()
    if up in ("MJPG", "YUY2"):
        try:
            fourcc = cv2.VideoWriter_fourcc(*up)
            cap.set(cv2.CAP_PROP_FOURCC, float(fourcc))
        except Exception:
            pass
    if set_fps is not None:
        try: cap.set(cv2.CAP_PROP_FPS, float(set_fps))
        except Exception: pass

def _should_quick_path(backend, codec, fps, fps_strategy):
    s = (fps_strategy or "auto").lower()
    be = (backend or "auto").lower()
    co = (codec or "AUTO").upper()
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

def _mean_brightness(frame):
    small = cv2.resize(frame, (64, 36), interpolation=cv2.INTER_AREA)
    return float(np.mean(small))

def _try_open_idx(idx: int) -> bool:
    cap = _try_open(idx)
    if cap is None:
        return False
    try: cap.release()
    except Exception: pass
    return True

def _pick_index_for_snap() -> int:
    """
    เลือกกล้องสำหรับ SNAP:
      1) พยายามใช้ FIXED_INDEX ก่อน
      2) ถ้าไม่ได้ ให้เลือก index ที่ 'สูงสุด' ที่เปิดได้ (คาดว่าเป็น USB)
      3) ถ้ายังไม่ได้ ให้เลือก index ที่ 'ต่ำสุด' ที่เปิดได้
      4) ถ้าไม่มีเลย -> raise 400
    """
    # 1) FIXED_INDEX ก่อน
    if 0 <= FIXED_INDEX <= SCAN_MAX and _try_open_idx(FIXED_INDEX):
        return FIXED_INDEX

    # 2) สแกนจากบนลงล่าง -> หาค่า "สูงสุด" ที่เปิดได้ (ชอบ USB)
    for idx in range(SCAN_MAX, -1, -1):
        if _try_open_idx(idx):
            return idx

    # (สำรอง) 3) ล่างขึ้นบน
    for idx in range(0, SCAN_MAX + 1):
        if _try_open_idx(idx):
            return idx

    # 4) ไม่เจอกล้องเลย
    raise HTTPException(status_code=400, detail="No available camera device")

def _backend_name(be: int) -> str:
    return (
        "dshow"        if be == getattr(cv2, "CAP_DSHOW", -1) else
        "msmf"         if be == getattr(cv2, "CAP_MSMF", -1) else
        "v4l2"         if be == getattr(cv2, "CAP_V4L2", -1) else
        "avfoundation" if be == getattr(cv2, "CAP_AVFOUNDATION", -1) else
        "auto" if be == 0 else str(be)
    )
    

# ===== Routes =====
@router.get("/ping", response_class=PlainTextResponse)
def ping():
    return "ok"

# --- Capture ---
@router.post("/capture", response_class=Response)
def capture(
    device_index: int = Query(0, ge=0, le=SCAN_MAX),
    width: int = Query(1280),
    height: int = Query(720),
    warmup: int = Query(3),
    fps: int = Query(15),
    jpeg_quality: int = Query(DEFAULT_JPEG_QUALITY),
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
    ok, frame = cap.read()
    cap.release()
    if not ok or frame is None or frame.size == 0:
        raise HTTPException(status_code=500, detail="Capture failed")
    data = _encode_jpeg(frame, quality=jpeg_quality)
    return Response(content=data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

# --- Preview ---
@router.get("/preview")
def preview(
    device_index: int = Query(0),
    width: int = Query(640),
    height: int = Query(480),
    warmup: int = Query(0),
    fps: int = Query(10),
    jpeg_quality: int = Query(DEFAULT_JPEG_QUALITY),
    backend: str = Query("dshow"),
    codec: str = Query("YUY2"),
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
    return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

# --- Status ---
@router.get("/status")
def camera_status(
    device_index: int = Query(0),
    width: int = Query(640),
    height: int = Query(360),
    fps: int = Query(DEFAULT_FPS),
    warmup: int = Query(2),
    backend: str = Query("dshow"),
    codec: str = Query("AUTO"),
    fps_strategy: str = Query("auto")
):
    cap = _open_cam_with_backend(device_index, backend)
    if cap is None:
        return {"status": "disconnected", "message": f"invalid index/backend (idx={device_index}, backend={backend})"}
    try:
        _apply_fourcc(cap, codec)
        use_quick, set_fps_value = _should_quick_path(backend, codec, fps, fps_strategy)
        if use_quick:
            _configure_cap_quick(cap, width, height, codec_hint=codec, set_fps=set_fps_value)
        else:
            _configure_cap_standard(cap, width, height, set_fps_value if set_fps_value else fps, codec=codec)
        _warmup(cap, warmup)
        ok, frame = cap.read()
        if not ok or frame is None:
            return {"status": "error", "message": "opened but cannot read frame"}
        h, w = frame.shape[:2]
        return {"status": "connected", "meta": {"width": w, "height": h, "fps": cap.get(cv2.CAP_PROP_FPS)}}
    finally:
        cap.release()

# --- Diagnostics ---
@router.get("/diag")
def camera_diag(
    device_index: int = Query(0),
    width: int = Query(640),
    height: int = Query(480),
    fps: int = Query(10),
    backend: str = Query("dshow"),
    codec: str = Query("AUTO"),
    fps_strategy: str = Query("auto")
):
    t0 = perf_counter()
    cap = _open_cam_with_backend(device_index, backend)
    t1 = perf_counter()
    if cap is None:
        return JSONResponse(status_code=400, content={"opened": False, "open_ms": int((t1 - t0) * 1000)})
    _apply_fourcc(cap, codec)
    use_quick, set_fps_value = _should_quick_path(backend, codec, fps, fps_strategy)
    if use_quick:
        _configure_cap_quick(cap, width, height, codec_hint=codec, set_fps=set_fps_value)
    else:
        _configure_cap_standard(cap, width, height, set_fps_value if set_fps_value else fps, codec=codec)
    t2 = perf_counter()
    ok, frame = cap.read()
    t3 = perf_counter()
    meta = {"opened": True, "open_ms": int((t1 - t0)*1000), "config_ms": int((t2 - t1)*1000),
            "first_frame_ms": int((t3 - t2)*1000), "total_ms": int((t3 - t0)*1000),
            "backend": backend, "codec": codec, "fps": fps, "used_quick_path": use_quick,
            "actual_fps": cap.get(cv2.CAP_PROP_FPS)}
    cap.release()
    return meta

# ===== Fixed-profile endpoints =====
FIXED_INDEX          = int(os.getenv("CAMERA_FIXED_INDEX", "1"))
FIXED_PREVIEW_BACK   = os.getenv("CAMERA_FIXED_PREVIEW_BACKEND", "dshow")
FIXED_PREVIEW_CODEC  = os.getenv("CAMERA_FIXED_PREVIEW_CODEC", "YUY2")
FIXED_PREVIEW_W      = int(os.getenv("CAMERA_FIXED_PREVIEW_W", "640"))
FIXED_PREVIEW_H      = int(os.getenv("CAMERA_FIXED_PREVIEW_H", "480"))
FIXED_PREVIEW_FPS    = int(os.getenv("CAMERA_FIXED_PREVIEW_FPS", "60"))
FIXED_PREVIEW_WARMUP = int(os.getenv("CAMERA_FIXED_PREVIEW_WARMUP", "0"))
FIXED_PREVIEW_STRAT  = os.getenv("CAMERA_FIXED_PREVIEW_FPS_STRATEGY", "auto")

FIXED_SNAP_BACK      = os.getenv("CAMERA_FIXED_SNAP_BACKEND", "msmf")
FIXED_SNAP_CODEC     = os.getenv("CAMERA_FIXED_SNAP_CODEC", "MJPG")
FIXED_SNAP_W         = int(os.getenv("CAMERA_FIXED_SNAP_W", "1280"))
FIXED_SNAP_H         = int(os.getenv("CAMERA_FIXED_SNAP_H", "720"))
FIXED_SNAP_FPS       = int(os.getenv("CAMERA_FIXED_SNAP_FPS", "15"))
FIXED_SNAP_WARMUP    = int(os.getenv("CAMERA_FIXED_SNAP_WARMUP", "3"))
FIXED_SNAP_STRAT     = os.getenv("CAMERA_FIXED_SNAP_FPS_STRATEGY", "auto")

FIXED_STATUS_BACK    = os.getenv("CAMERA_FIXED_STATUS_BACKEND", "dshow")
FIXED_STATUS_CODEC   = os.getenv("CAMERA_FIXED_STATUS_CODEC", "AUTO")
FIXED_STATUS_W       = int(os.getenv("CAMERA_FIXED_STATUS_W", "640"))
FIXED_STATUS_H       = int(os.getenv("CAMERA_FIXED_STATUS_H", "360"))
FIXED_STATUS_FPS     = int(os.getenv("CAMERA_FIXED_STATUS_FPS", str(DEFAULT_FPS)))
FIXED_STATUS_WARMUP  = int(os.getenv("CAMERA_FIXED_STATUS_WARMUP", "2"))
FIXED_STATUS_STRAT   = os.getenv("CAMERA_FIXED_STATUS_FPS_STRATEGY", "auto")
FIXED_TOKEN          = os.getenv("CAMERA_FIXED_TOKEN", "").strip()

def _qs(params: dict):
    from urllib.parse import urlencode
    return urlencode(params)

def _require_token(x_token: Optional[str] = Header(default=None, alias="X-Token")):
    if not FIXED_TOKEN:
        return
    if x_token != FIXED_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")

@router.get("/live")
def live(_: None = Depends(_require_token)):
    chosen_idx = _pick_index_for_snap()
    qs = _qs({
        "device_index": chosen_idx,
        "backend": FIXED_PREVIEW_BACK, "codec": FIXED_PREVIEW_CODEC,
        "width": FIXED_PREVIEW_W, "height": FIXED_PREVIEW_H,
        "fps": FIXED_PREVIEW_FPS, "warmup": FIXED_PREVIEW_WARMUP,
        "fps_strategy": FIXED_PREVIEW_STRAT
    })
    return RedirectResponse(url=f"/camera/preview?{qs}", status_code=302)

@router.get("/live_page", response_class=HTMLResponse)
def live_page(_: None = Depends(_require_token)):
    html = """
    <html><head><meta http-equiv="Cache-Control" content="no-store" /></head>
    <body style="margin:0;background:#111;color:#eee;font-family:monospace">
      <div style="padding:8px">Preview: /camera/live</div>
      <img src="/camera/live" style="display:block;max-width:100vw;max-height:100vh"/>
    </body></html>"""
    return HTMLResponse(content=html, headers={"Cache-Control": "no-store"})

@router.api_route("/snap", methods=["GET", "POST"])
def snap(_: None = Depends(_require_token)):
    """
    ถ่ายรูปนิ่งด้วยโปรไฟล์คงที่ (ไม่มี X-Token ก็ได้ ถ้าไม่ได้ตั้ง FIXED_TOKEN)
    ตอนนี้จะเลือก index ตาม _pick_index_for_snap() เพื่อ 'ชอบ USB' และ fallback อัตโนมัติ
    """
    try:
        chosen_idx = _pick_index_for_snap()
    except HTTPException as e:
        # ส่งต่อ error
        raise e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Pick camera index failed: {e}")

    # ใช้พารามิเตอร์คงที่ของ SNAP แต่แทน device_index ด้วย chosen_idx
    return capture(
        device_index=chosen_idx,
        width=FIXED_SNAP_W,
        height=FIXED_SNAP_H,
        warmup=FIXED_SNAP_WARMUP,
        fps=FIXED_SNAP_FPS,
        jpeg_quality=DEFAULT_JPEG_QUALITY,
        backend=FIXED_SNAP_BACK,
        codec=FIXED_SNAP_CODEC,
        fps_strategy=FIXED_SNAP_STRAT,
    )

@router.get("/health")
def health(_: None = Depends(_require_token)):
    chosen_idx = _pick_index_for_snap()
    qs = _qs({
        "device_index": chosen_idx,
        "backend": FIXED_STATUS_BACK, "codec": FIXED_STATUS_CODEC,
        "width": FIXED_STATUS_W, "height": FIXED_STATUS_H,
        "fps": FIXED_STATUS_FPS, "warmup": FIXED_STATUS_WARMUP,
        "fps_strategy": FIXED_STATUS_STRAT
    })
    return RedirectResponse(url=f"/camera/status?{qs}", status_code=302)


@router.get("/devices")
def list_devices(
    include_closed: bool = Query(False, description="ถ้า true จะแสดง index ทั้งหมดแม้เปิดไม่ได้"),
    quick: bool = Query(True, description="เปิดแบบเร็ว: แค่เช็คเปิดได้และอ่าน meta เบื้องต้น"),
    try_config: bool = Query(False, description="ลอง set W/H/FPS คร่าว ๆ เพื่อลองเจรจา format"),
    probe_width: int = Query(640, ge=1),
    probe_height: int = Query(480, ge=1),
    probe_fps: int = Query(15, ge=1),
):
    devices = []
    platform_backends = _backends_for_platform()

    for idx in range(0, SCAN_MAX + 1):
        opened = False
        opened_backend = None
        width = height = None
        fps = None

        for be in platform_backends:
            cap = cv2.VideoCapture(idx, be)
            if cap is not None and cap.isOpened():
                opened = True
                opened_backend = _backend_name(be)

                # ปรับแบบเร็ว (option) เพื่อกันค้าง
                if try_config:
                    try:
                        cap.set(cv2.CAP_PROP_FRAME_WIDTH,  float(probe_width))
                        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, float(probe_height))
                        cap.set(cv2.CAP_PROP_FPS,          float(probe_fps))
                        if hasattr(cv2, "CAP_PROP_BUFFERSIZE"):
                            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                    except Exception:
                        pass

                # อ่าน meta ที่กล้องรายงาน
                try:
                    width  = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
                    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
                    fps    = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
                except Exception:
                    pass

                try: cap.release()
                except Exception: pass
                break  # เปิดได้แล้ว ไม่ต้องลอง backend ต่อ
            try: cap.release()
            except Exception: pass

        item = {
            "index": idx,
            "opened": opened,
            "backend": opened_backend,  # อาจเป็น None ถ้าเปิดไม่ได้
            "width": width,
            "height": height,
            "fps": fps,
        }
        if opened or include_closed:
            devices.append(item)

    return {
        "scan_max": SCAN_MAX,
        "os_backends": [_backend_name(b) for b in platform_backends],
        "devices": devices
    }