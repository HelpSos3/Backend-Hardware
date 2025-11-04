from fastapi import APIRouter, Query
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
import os, re, time, serial

router = APIRouter(prefix="/scale", tags=["scale"])

class ScaleReadResponse(BaseModel):
    weight: Optional[float] = None
    unit: Optional[str] = None
    stable: Optional[bool] = None
    raw: List[str]
    ts: float
    meta: Dict[str, Any]

A12E_PATTERN = re.compile(
    r"\b(?P<stable>ST|US|OL)\s*,?\s*(?P<mode>GS|NT|TR)?\s*,?\s*"
    r"(?P<value>[+-]?\d+(?:\.\d+)?)\s*(?P<unit>[a-zA-Z]+)?",
    re.IGNORECASE,
)

def parse_a12e_line(line: str):
    m = A12E_PATTERN.search(line)
    if not m:
        return None, None, None
    stable = m.group("stable").upper() == "ST"
    try:
        value = float(m.group("value"))
    except:
        value = None
    unit = (m.group("unit") or "").strip().lower() or "kg"
    return value, unit, stable

@router.get("/read", response_model=ScaleReadResponse)
def read_scale(
    port: str = Query(os.getenv("SCALE_PORT", "COM3")),
    baud: int = Query(int(os.getenv("SCALE_BAUD", "9600"))),
    timeout_ms: int = Query(500),
    lines: int = Query(5),
):
    timeout_s = timeout_ms / 1000.0
    raw = []
    with serial.Serial(port, baudrate=baud, timeout=timeout_s) as ser:
        for _ in range(lines):
            line = ser.readline().decode(errors="ignore").strip()
            if line:
                raw.append(line)
    weight, unit, stable = None, None, None
    for ln in reversed(raw):
        w, u, s = parse_a12e_line(ln)
        if w is not None:
            weight, unit, stable = w, u, s
            break
    return ScaleReadResponse(
        weight=weight,
        unit=unit,
        stable=stable,
        raw=raw,
        ts=time.time(),
        meta={"port": port, "baud": baud, "timeout_ms": timeout_ms, "lines": lines},
    )
