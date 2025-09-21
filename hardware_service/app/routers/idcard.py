# app/routers/idcard.py
from fastapi import APIRouter, HTTPException, Query
import base64, os, requests

from ..devices.idcard_thai.ThaiCIDHelper import ThaiCIDHelper, searchDATAValue, searchAPDUPhoto
from ..devices.idcard_thai.DataThaiCID import (
    APDU_SELECT, APDU_THAI_CARD, APDU_PHOTO, ThaiCIDDataType
)

router = APIRouter()

def _read_field(helper: ThaiCIDHelper, key: str, data_type: ThaiCIDDataType) -> str:
    apdu = searchDATAValue('key', key, 'apdu')
    if not apdu:
        return ""
    text_val, _raw = helper.getValue(apdu, data_type)
    return (text_val or "").strip()

def _scan_core(reader_index: int, with_photo: int):
    helper = ThaiCIDHelper(APDU_SELECT, APDU_THAI_CARD)

    if not helper.cardReaderList:
        raise RuntimeError("ไม่พบเครื่องอ่านบัตร (PC/SC).")

    _conn, ok = helper.connectReader(reader_index)
    if not ok:
        raise RuntimeError(helper.lastError or "เชื่อมต่อเครื่องอ่านไม่ได้")

    # ส่ง SELECT แบบตรง ๆ เพื่อให้การ์ดอยู่ใน ADF ที่ถูกต้อง
    _data, sw1, sw2 = helper.cardReader.transmit(APDU_SELECT + APDU_THAI_CARD)
    if not (sw1 == 0x61 or sw1 == 0x90):
        raise RuntimeError(f"SELECT ไม่สำเร็จ (SW1={sw1:02X}, SW2={sw2:02X})")

    # อ่านข้อมูลหลัก
    national_id = _read_field(helper, "APDU_CID", ThaiCIDDataType.TEXT).replace("-", "")
    full_name   = _read_field(helper, "APDU_THFULLNAME", ThaiCIDDataType.NAME)
    address     = _read_field(helper, "APDU_ADDRESS", ThaiCIDDataType.ADDRESS)

    # ลองพิมพ์ดีบั๊กเล็กน้อย (ดูในคอนโซล)
    print(f"[DEBUG] CID='{national_id}' NAME='{full_name}' ADDR.len={len(address)}")

    photo_b64 = None
    if with_photo == 1:
        photo_bytes = []
        for part in APDU_PHOTO:
            apdu = searchAPDUPhoto(part["key"])
            if apdu:
                photo_bytes += helper.getPhoto(apdu)
        if photo_bytes:
            photo_b64 = base64.b64encode(bytes(photo_bytes)).decode("ascii")

    # ให้ผ่านได้ถ้ามีอย่างน้อย ‘อย่างใดอย่างหนึ่ง’
    if not (national_id or full_name or address):
        raise RuntimeError("อ่านบัตรไม่สำเร็จ (ข้อมูลว่าง)")

    return {
        "national_id": national_id,
        "full_name": full_name,
        "address": address,
        "photo_base64": photo_b64,
    }



@router.get("/scan")
def scan(reader_index: int = Query(0, ge=0), with_photo: int = Query(1)):
    try:
        return _scan_core(reader_index, with_photo)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"scan error: {e}")

