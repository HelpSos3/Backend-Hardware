# hardware_service/app/routers/camera.py
from fastapi import APIRouter, Response, HTTPException, Query
from fastapi.responses import StreamingResponse, PlainTextResponse , JSONResponse
import cv2, time, numpy as np
from typing import Generator, Optional

router = APIRouter(prefix="/camera", tags=["camera"])

def _open_cam(device_index: int) -> Optional[cv2.VideoCapture]:
    for backend in (cv2.CAP_DSHOW, cv2.CAP_MSMF, 0):
        cap = cv2.VideoCapture(device_index, backend)
        if cap is not None and cap.isOpened():
            return cap
        try:
            cap.release()
        except Exception:
            pass
    return None

@router.get("/ping", response_class=PlainTextResponse)
def ping():
    return "ok"

@router.post("/capture", response_class=Response)
def capture(
    device_index: int = Query(0),
    width: int = Query(1280, ge=160, le=3840),
    height: int = Query(720, ge=120, le=2160),
    warmup: int = Query(8, ge=0, le=30),
):
    cap = _open_cam(device_index)
    if cap is None:
        raise HTTPException(status_code=500, detail="Cannot open camera")

    cap.set(cv2.CAP_PROP_FRAME_WIDTH,  float(width))
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, float(height))
    cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 0.75)
    cap.set(cv2.CAP_PROP_AUTOFOCUS, 1)

    for _ in range(warmup):
        cap.read()
        time.sleep(0.06)

    ok, frame = cap.read()
    cap.release()
    if not ok or frame is None:
        raise HTTPException(status_code=500, detail="Capture failed")
    if np.mean(frame) < 2:
        raise HTTPException(status_code=503, detail="Frame too dark; increase warmup or lighting")

    ok, buf = cv2.imencode(".jpg", frame)
    if not ok:
        raise HTTPException(status_code=500, detail="JPEG encode failed")
    return Response(content=buf.tobytes(), media_type="image/jpeg")

@router.get("/preview")
def preview(
    device_index: int = Query(0),
    width: int = Query(1280, ge=160, le=3840),
    height: int = Query(720, ge=120, le=2160),
    warmup: int = Query(8, ge=0, le=60),
    fps: int = Query(15, ge=1, le=60),
):
    cap = _open_cam(device_index)
    if cap is None:
        raise HTTPException(status_code=500, detail="Cannot open camera")

    cap.set(cv2.CAP_PROP_FRAME_WIDTH,  float(width))
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, float(height))
    cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 0.75)
    cap.set(cv2.CAP_PROP_AUTOFOCUS, 1)

    for _ in range(warmup):
        cap.read()
        time.sleep(0.06)

    delay = 1.0 / float(fps)

    def gen() -> Generator[bytes, None, None]:
        try:
            while True:
                ok, frame = cap.read()
                if not ok or frame is None:
                    # ส่งเฟรมว่างสีดำกันจอดับ พร้อมปริ้น log
                    black = np.zeros((int(height), int(width), 3), dtype=np.uint8)
                    ok2, buf2 = cv2.imencode(".jpg", black)
                    chunk = buf2.tobytes() if ok2 else b""
                else:
                    # กันภาพมืดจัด
                    if np.mean(frame) < 2:
                        frame[:] = (0, 0, 0)
                    ok2, buf2 = cv2.imencode(".jpg", frame)
                    chunk = buf2.tobytes() if ok2 else b""

                yield (b"--frame\r\n"
                       b"Content-Type: image/jpeg\r\n\r\n" + chunk + b"\r\n")
                time.sleep(delay)
        except GeneratorExit:
            # เบราเซอร์ตัดการเชื่อมต่อ
            pass
        finally:
            try:
                cap.release()
            except Exception:
                pass

    return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame")


@router.get("/status")
def camera_status(device_index: int = Query(0, description="index กล้อง 0,1,...")):
    cap = _open_cam(device_index)
    if cap is None:
        return {"status": "disconnected", "message": "ไม่สามารถเชื่อมต่อกล้องได้"}
    try:
        ok, _ = cap.read()
        if not ok:
            return {"status": "error", "message": "เปิดกล้องได้ แต่ไม่สามารถอ่านภาพได้"}
        return {"status": "connected", "message": "กล้องพร้อมใช้งาน"}
    finally:
        try:
            cap.release()
        except Exception:
            pass


