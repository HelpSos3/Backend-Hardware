from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse , JSONResponse
from pydantic import BaseModel
from typing import List, Optional
import os, datetime, io
import base64 as _b64

# ===== Optional ESC/POS text mode deps =====
try:
    import win32print
except Exception:
    win32print = None

# ===== Pillow for image mode =====
try:
    from PIL import Image, ImageDraw, ImageFont
except Exception:
    Image = ImageDraw = ImageFont = None

router = APIRouter(prefix="/printer", tags=["printer"])

# ===================== Models =====================
class ReceiptItem(BaseModel):
    name: str
    qty: float
    unit: str = "kg"
    price: float

class ReceiptData(BaseModel):
    store_name: str = "ร้านรับซื้อของเก่า"
    receipt_no: str
    customer_name: str = "ไม่ระบุ"
    items: List[ReceiptItem]
    total: float

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from PIL import Image as PILImage
    from PIL import ImageDraw as PILDraw
    from PIL import ImageFont as PILFont

# ===================== Config =====================
def _cfg_printer_name() -> str:
    env = os.getenv("PRINTER_NAME", "").strip()
    if env:
        return env
    if not win32print:
        raise RuntimeError("pywin32 not available")
    return win32print.GetDefaultPrinter()

def _cfg_codepage() -> int:
    try:
        return int(os.getenv("PRINTER_CP", "26"))   # เดิม text mode ใช้ 26
    except Exception:
        return 26

def _cfg_encoding() -> str:
    return os.getenv("PRINTER_ENC", "cp874").strip() or "cp874"

def _cfg_width_chars() -> int:
    try:
        return int(os.getenv("RECEIPT_WIDTH", "32"))  # สำหรับโหมด text
    except Exception:
        return 32

def _cfg_mode() -> str:
    # 'image' (แนะนำ) หรือ 'text'
    return (os.getenv("RECEIPT_MODE", "image") or "image").lower()

def _cfg_img_width_px() -> int:
    # 58mm มัก ~384px, 80mm มัก ~576px
    try:
        return int(os.getenv("RECEIPT_PX_WIDTH", "384"))
    except Exception:
        return 384

def _cfg_font_path() -> Optional[str]:
    # เลือก font ไทยอัตโนมัติ หากไม่ระบุ
    env = os.getenv("FONT_PATH", "").strip()
    if env and os.path.isfile(env):
        return env
    candidates = [
        # Windows ฝั่งไทยส่วนใหญ่มี Sarabun
        r"C:\Windows\Fonts\THSarabunNew.ttf",
        r"C:\Windows\Fonts\thsarabunnew.ttf",
        r"C:\Windows\Fonts\Kanit-Regular.ttf",
        r"C:\Windows\Fonts\Tahoma.ttf",
        # Noto
        r"C:\Windows\Fonts\NotoSansThai-Regular.ttf",
        r"/usr/share/fonts/truetype/noto/NotoSansThai-Regular.ttf",
        r"/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",  # สำรอง (มีไทย)
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    return None

def _cfg_dpi() -> int:
    # Thermal ทั่วไป 203 DPI (8 dot/mm). ถ้าเครื่องคุณ 300 DPI ก็เปลี่ยนเป็น 300
    try:
        return int(os.getenv("RECEIPT_DPI", "203"))
    except Exception:
        return 203

# ===================== ESC/POS helpers (text mode) =====================
ESC = b"\x1b"
GS  = b"\x1d"

def _encode(s: str) -> bytes:
    return s.encode(_cfg_encoding(), errors="replace")

def _line(s: str) -> bytes:
    return _encode(s.rstrip("\r\n") + "\r\n")

def _fmt_line_lr(left: str, right: str, width: int = None) -> str:
    w = width or _cfg_width_chars()
    left = left.strip()
    right = right.strip()
    space = max(1, w - len(left) - len(right))
    return left + (" " * space) + right

def _esc_init() -> bytes: return ESC + b"@"
def _esc_codepage(cp: int): return ESC + b"t" + bytes([cp])
def _esc_align(center=False): return ESC + b"a" + (b"\x01" if center else b"\x00")
def _esc_double(on: bool): return GS + b"!" + (b"\x11" if on else b"\x00")
def _esc_feed(lines: int = 1) -> bytes: return b"\x0A" * lines
def _esc_cut(): return GS + b"V" + b"\x01"

def _esc_set_default_state() -> bytes:
    return b"".join([
        _esc_init(),
        _esc_align(False),
        GS + b"!" + b"\x00",  # normal font
        ESC + b"2",           # default line spacing
    ])

def _start_doc(name: str):
    h = win32print.OpenPrinter(name)
    win32print.StartDocPrinter(h, 1, ("Receipt", None, "RAW"))
    win32print.StartPagePrinter(h)
    return h

def _end_doc(h):
    win32print.EndPagePrinter(h)
    win32print.EndDocPrinter(h)
    win32print.ClosePrinter(h)

def _write(h, data: bytes): win32print.WritePrinter(h, data)


# ===================== TEXT MODE builder =====================
def build_receipt_bytes_text(data: ReceiptData) -> bytes:
    cp = _cfg_codepage()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    width = _cfg_width_chars()

    buf = b""
    buf += _esc_set_default_state()
    buf += _esc_codepage(cp)

    # Header
    buf += _esc_align(True)
    buf += _esc_double(True)
    buf += _line(data.store_name)
    buf += _esc_double(False)
    buf += _esc_align(False)
    buf += _line("-" * width)
    buf += _line(f"เลขที่ใบเสร็จ: {data.receipt_no}")
    buf += _line(f"วันที่: {now}")
    buf += _line(f"ลูกค้า: {data.customer_name or 'ไม่ระบุ'}")
    buf += _line("-" * width)

    # Items
    for it in data.items:
        amount = it.qty * it.price
        # ตัดชื่อหลายบรรทัดถ้ายาว
        for i in range(0, len(it.name), width):
            buf += _line(it.name[i:i+width])
        left = f"  {it.qty:g}{it.unit} x {it.price:,.2f}"
        right = f"{amount:,.2f}"
        buf += _line(_fmt_line_lr(left, right, width))

    # Total
    buf += _line("-" * width)
    buf += _esc_align(True)
    buf += _esc_double(True)
    buf += _line(f"ยอดรวมทั้งหมด {data.total:,.2f} บาท")
    buf += _esc_double(False)
    buf += _esc_align(False)
    buf += _esc_feed(3)
    buf += _esc_cut()
    return buf


def _image_to_jpeg_bytes(img: "PILImage.Image", quality: int = 90) -> bytes:
    _ensure_pillow()
    buf = io.BytesIO()
    dpi = _cfg_dpi()
    # ฝัง DPI เพื่อให้พิมพ์ “ขนาดจริง”
    img.convert("L").save(buf, format="JPEG", quality=quality, optimize=True, dpi=(dpi, dpi))
    return buf.getvalue()


# ===================== IMAGE MODE (recommended) =====================
def _ensure_pillow():
    if Image is None or ImageDraw is None or ImageFont is None:
        raise HTTPException(500, "Pillow is not installed. Run: pip install pillow")

def _load_font(size: int) -> PILFont.FreeTypeFont:
    _ensure_pillow()
    font_path = _cfg_font_path()
    if not font_path:
        raise HTTPException(500, "Thai font not found. Set FONT_PATH to a .ttf (e.g., THSarabunNew.ttf).")
    try:
        return ImageFont.truetype(font_path, size=size)
    except Exception as e:
        raise HTTPException(500, f"Cannot load font: {font_path} ({e})")

def _text_size(draw: PILDraw.ImageDraw, text: str, font: PILFont.ImageFont) -> tuple:
    # ใช้ getlength ถ้ามี เพื่อความแม่นสำหรับอักษรไทย
    if hasattr(font, "getlength"):
        return (int(font.getlength(text)), font.size)
    else:
        return draw.textsize(text, font=font)

def build_receipt_image(data: ReceiptData, width_px: int) -> PILImage.Image:
    """
    เรนเดอร์ใบเสร็จเป็นภาพขาวดำ (1-bit) สำหรับ ESC/POS raster
    """
    _ensure_pillow()
    # ขนาดตัวอักษรตั้งตามความกว้าง
    # 384px (58mm) ใช้ 28-32 ได้; 576px (80mm) ใช้ 34-40
    if width_px <= 384:
        f_big = _load_font(32)
        f_norm = _load_font(26)
        f_small = _load_font(24)
    else:
        f_big = _load_font(40)
        f_norm = _load_font(32)
        f_small = _load_font(28)

    # เตรียม canvas สูงๆ ก่อน แล้วค่อย crop
    H = 2000
    img = Image.new("L", (width_px, H), 255)
    draw = ImageDraw.Draw(img)

    x = 0
    y = 10
    pad = 6
    sep = "-" * 64

    # Center helper
    def draw_center(text: str, font):
        nonlocal y
        w, _ = _text_size(draw, text, font)
        draw.text(((width_px - w)//2, y), text, font=font, fill=0)
        y += font.size + pad

    def draw_left(text: str, font):
        nonlocal y
        draw.text((x, y), text, font=font, fill=0)
        y += font.size + pad

    def draw_lr(left: str, right: str, font):
        nonlocal y
        lw, _ = _text_size(draw, left, font)
        rw, _ = _text_size(draw, right, font)
        draw.text((x, y), left, font=font, fill=0)
        draw.text((width_px - rw, y), right, font=font, fill=0)
        y += font.size + pad

    # header
    draw_center(data.store_name, f_big)
    draw_left(sep, f_small)
    draw_left(f"เลขที่ใบเสร็จ: {data.receipt_no}", f_norm)
    draw_left(f"วันที่: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}", f_norm)
    draw_left(f"ลูกค้า: {data.customer_name or 'ไม่ระบุ'}", f_norm)
    draw_left(sep, f_small)

    # items (wrap ชื่อสินค้าให้พอดีความกว้าง)
    # กำหนดความกว้างอักษรโดยคร่าว ๆ (ใช้ wrap width ตาม pixel)
    max_name_px = width_px - 10
    def wrap_by_pixel(text: str, font):
        # แบ่งคำแบบประมาณจากความกว้างพิกเซล
        words = text.split(" ")
        lines, cur = [], ""
        for w_ in words:
            test = (cur + " " + w_).strip()
            tw, _ = _text_size(draw, test, font)
            if tw <= max_name_px:
                cur = test
            else:
                if cur:
                    lines.append(cur)
                cur = w_
        if cur:
            lines.append(cur)
        # ถ้าขีดคั่นด้วยไม่มีเว้นวรรค (ภาษาไทยล้วน) ให้ตกมาทีละตัว
        if not lines:
            lines = [text]
        return lines

    for it in data.items:
        name_lines = wrap_by_pixel(it.name, f_norm)
        for ln in name_lines:
            draw_left(ln, f_norm)
        left = f"  {it.qty:g}{it.unit} x {it.price:,.2f}"
        right = f"{(it.qty * it.price):,.2f}"
        draw_lr(left, right, f_norm)

    draw_left(sep, f_small)
    draw_center(f"ยอดรวมทั้งหมด {data.total:,.2f} บาท", f_big)

    # crop ส่วนเกิน
    y_end = min(y + 40, H)
    img = img.crop((0, 0, width_px, y_end))

    # แปลงเป็น 1-bit dithered สำหรับความคมชัด
    img = img.convert("1")
    return img



def _image_to_escpos_raster(img: "PILImage.Image") -> bytes:
    """
    แปลงภาพ 1-bit เป็นคำสั่ง ESC/POS raster:
    GS v 0 m xL xH yL yH [data]
    """
    if img.mode != "1":
        img = img.convert("1")
    width, height = img.size
    row_bytes = (width + 7) // 8
    data = bytearray()

    # Header
    xL = row_bytes & 0xFF
    xH = (row_bytes >> 8) & 0xFF
    yL = height & 0xFF
    yH = (height >> 8) & 0xFF

    # คำสั่ง raster bit image, mode m=0 (normal)
    data += GS + b"v0" + b"\x00" + bytes([xL, xH, yL, yH])

    # Pixel packing: 1 = black (dot), MSB first
    pixels = img.load()
    for yy in range(height):
        byte = 0
        bit = 7
        for xx in range(width):
            # ในโหมด "1": 0 = white, 255 = black (ขึ้นกับ PIL)
            # เช็คว่าเป็นจุดดำ?
            p = pixels[xx, yy]
            is_black = (p == 0)  # สำหรับ PIL "1", black = 0
            if is_black:
                byte |= (1 << bit)
            bit -= 1
            if bit < 0:
                data.append(byte)
                byte = 0
                bit = 7
        if bit != 7:
            data.append(byte)

    # feed + cut
    data += b"\n\n\n" + GS + b"V" + b"\x01"
    return bytes(data)


# ===================== API =====================
@router.post("/receipt")
def print_receipt(data: ReceiptData):
    if not win32print:
        raise HTTPException(500, "pywin32 not available")

    name = _cfg_printer_name()
    h = _start_doc(name)
    try:
        mode = _cfg_mode()
        if mode == "text":
            # โหมดเดิม (อาจไม่รองรับไทยกับเครื่องบางรุ่น)
            payload = build_receipt_bytes_text(data)
            _write(h, payload)
        else:
            # โหมดภาพ (แนะนำ) – ชัวร์สุดสำหรับภาษาไทย
            if Image is None:
                raise HTTPException(500, "Pillow not installed. pip install pillow")

            width_px = _cfg_img_width_px()
            img = build_receipt_image(data, width_px)
            escpos = _image_to_escpos_raster(img)
            _write(h, escpos)
    finally:
        _end_doc(h)

    return {"status": "ok", "printer": name, "mode": _cfg_mode()}

@router.post("/render")
def render_receipt_image(data: ReceiptData):
    """
    เรนเดอร์ใบเสร็จเป็นภาพ JPEG (โดยไม่สั่งพิมพ์)
    เหมาะให้ backend ดึงไปเก็บไฟล์แนบในฐานข้อมูล/อัปโหลด
    """
    width_px = _cfg_img_width_px()
    img = build_receipt_image(data, width_px)
    jpeg_bytes = _image_to_jpeg_bytes(img)

    return StreamingResponse(
        io.BytesIO(jpeg_bytes),
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store"}
    )

@router.post("/render_base64")
def render_receipt_image_base64(data: ReceiptData):
    """
    เหมือน /render แต่ห่อเป็น base64 ใน JSON
    """
    width_px = _cfg_img_width_px()
    img = build_receipt_image(data, width_px)
    jpeg_bytes = _image_to_jpeg_bytes(img)
    b64 = _b64.b64encode(jpeg_bytes).decode("ascii")
    return JSONResponse({"image_base64": b64, "mime": "image/jpeg"})



