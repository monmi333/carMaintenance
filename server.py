#!/usr/bin/env python3
"""자동차 정비 관리 웹 애플리케이션 서버.

외부 패키지 없이 Python 표준 라이브러리(http.server, sqlite3 등)만 사용한다.
실행: python server.py [--host 127.0.0.1] [--port 8000]
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
import re
import secrets
import sqlite3
import struct
import sys
import traceback
from collections import defaultdict
from contextlib import closing
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
UPLOAD_DIR = BASE_DIR / "uploads"
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "car_maintenance.db"

MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 업로드 파일 최대 5MB
MAX_IMPORT_BYTES = 10 * 1024 * 1024
MULTIPART_OVERHEAD = 64 * 1024      # 폼 필드·경계 문자열 여유분
MAX_JSON_BYTES = 64 * 1024

ALLOWED_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp")
# 허용 목록에 없는 확장자는 모두 거부하지만, 아래 확장자는 '위험한 확장자'로 따로 안내한다.
DANGEROUS_EXTENSIONS = (".exe", ".msi", ".bat", ".cmd", ".com", ".scr", ".pif", ".dll", ".ps1", ".vbs",
                        ".js", ".jar", ".sh", ".php", ".py", ".html", ".htm", ".svg", ".hta", ".lnk")
WINDOWS_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                          *(f"LPT{i}" for i in range(1, 10))}

TIME_SLOTS = [f"{hour:02d}:00" for hour in range(9, 18)]  # 09:00 ~ 17:00, 1시간 단위
MAX_ADVANCE_DAYS = 90         # 오늘부터 며칠 뒤까지 예약을 받을지
PENDING_TTL_MINUTES = 15      # 결제 대기 예약이 시간대를 잡아두는 시간. 지나면 자동 취소
REQUEST_TIMEOUT_SECONDS = 30  # 소켓 읽기 타임아웃 (느린 연결로 스레드를 붙잡는 공격 방지)

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".ico": "image/x-icon",
    ".svg": "image/svg+xml",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS maintenance_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    part_name   TEXT    NOT NULL,
    job_name    TEXT    NOT NULL,
    price       INTEGER NOT NULL CHECK (price >= 0),
    interval_km INTEGER NOT NULL CHECK (interval_km > 0)
);

CREATE TABLE IF NOT EXISTS reservations (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_name  TEXT    NOT NULL,
    phone          TEXT    NOT NULL,
    car_number     TEXT    NOT NULL,
    reserve_date   TEXT    NOT NULL,
    reserve_time   TEXT    NOT NULL,
    memo           TEXT    NOT NULL DEFAULT '',
    total_price    INTEGER NOT NULL,
    status         TEXT    NOT NULL DEFAULT 'PENDING'
                   CHECK (status IN ('PENDING', 'PAID', 'CANCELLED')),
    payment_method TEXT,
    transaction_id TEXT,
    paid_at        TEXT,
    created_at     TEXT    NOT NULL
);

-- 취소되지 않은 예약끼리는 같은 날짜·시간대를 중복 예약할 수 없다.
CREATE UNIQUE INDEX IF NOT EXISTS ux_reservations_slot
    ON reservations (reserve_date, reserve_time) WHERE status != 'CANCELLED';

CREATE TABLE IF NOT EXISTS reservation_items (
    reservation_id INTEGER NOT NULL REFERENCES reservations (id) ON DELETE CASCADE,
    item_id        INTEGER NOT NULL REFERENCES maintenance_items (id),
    price          INTEGER NOT NULL,  -- 예약 시점 가격 스냅샷
    PRIMARY KEY (reservation_id, item_id)
);

CREATE TABLE IF NOT EXISTS vehicles (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    car_number TEXT    NOT NULL UNIQUE,
    model      TEXT    NOT NULL,
    mileage    INTEGER NOT NULL DEFAULT 0,
    photo      TEXT,
    updated_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS vehicle_records (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    car_number  TEXT    NOT NULL,
    record_date TEXT    NOT NULL,
    record_type TEXT    NOT NULL CHECK (record_type IN ('정비', '주유', '기타', '주행')),
    detail      TEXT    NOT NULL,
    amount      INTEGER NOT NULL DEFAULT 0,
    mileage     INTEGER,
    place       TEXT    NOT NULL DEFAULT '',
    memo        TEXT    NOT NULL DEFAULT '',
    source_key  TEXT    NOT NULL UNIQUE,
    created_at  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_vehicle_records_car_date
    ON vehicle_records (car_number, record_date DESC, id DESC);
"""

SEED_ITEMS = [
    ("엔진오일", "엔진오일 및 오일필터 교환", 65000, 7500),
    ("에어클리너 필터", "엔진 흡기 필터 교환", 25000, 15000),
    ("에어컨 필터", "실내 공조 필터 교환", 20000, 10000),
    ("브레이크 패드", "전륜 브레이크 패드 교환", 120000, 30000),
    ("브레이크 오일", "브레이크액 전량 교환", 50000, 40000),
    ("타이어", "타이어 4본 교환 및 휠 밸런스", 480000, 50000),
    ("타이어 위치교환", "타이어 로테이션", 30000, 10000),
    ("배터리", "배터리 교환 및 충전 계통 점검", 150000, 60000),
    ("점화플러그", "점화플러그 4개 교환", 80000, 40000),
    ("냉각수", "부동액 교환 및 냉각 계통 점검", 60000, 40000),
    ("미션오일", "자동변속기 오일 교환", 150000, 60000),
    ("타이밍벨트", "타이밍벨트 및 텐셔너 교환", 350000, 80000),
]


class ApiError(Exception):
    """클라이언트에 JSON 오류 응답으로 전달되는 예외."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


# ---------------------------------------------------------------------------
# 데이터베이스
# ---------------------------------------------------------------------------

def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_storage() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    UPLOAD_DIR.mkdir(exist_ok=True)
    with closing(connect()) as conn:
        conn.execute("PRAGMA journal_mode = WAL")  # 읽기와 쓰기가 서로 막지 않도록 (DB 파일에 유지됨)
        with conn:
            conn.executescript(SCHEMA)
            if conn.execute("SELECT COUNT(*) FROM maintenance_items").fetchone()[0] == 0:
                conn.executemany(
                    "INSERT INTO maintenance_items (part_name, job_name, price, interval_km) VALUES (?, ?, ?, ?)",
                    SEED_ITEMS,
                )


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def fetch_reservations(conn: sqlite3.Connection, reservation_id: int | None = None) -> list[dict]:
    where, params = ("WHERE r.id = ?", (reservation_id,)) if reservation_id is not None else ("", ())
    rows = conn.execute(
        f"""SELECT r.*, v.photo AS vehicle_photo, v.model AS vehicle_model
              FROM reservations r
              LEFT JOIN vehicles v ON v.car_number = r.car_number
              {where}
             ORDER BY r.reserve_date DESC, r.reserve_time DESC, r.id DESC""",
        params,
    ).fetchall()
    if not rows:
        return []

    ids = [row["id"] for row in rows]
    items_by_reservation: dict[int, list[dict]] = defaultdict(list)
    for item in conn.execute(
        f"""SELECT ri.reservation_id, ri.item_id, ri.price, m.part_name, m.job_name
              FROM reservation_items ri
              JOIN maintenance_items m ON m.id = ri.item_id
             WHERE ri.reservation_id IN ({",".join("?" * len(ids))})
             ORDER BY m.id""",
        ids,
    ):
        items_by_reservation[item["reservation_id"]].append(
            {k: item[k] for k in ("item_id", "part_name", "job_name", "price")}
        )

    return [serialize_reservation(row, items_by_reservation[row["id"]]) for row in rows]


def serialize_reservation(row: sqlite3.Row, items: list[dict]) -> dict:
    data = {**dict(row), "items": items, "phone": mask_phone(row["phone"]), "payment_deadline": None}
    if row["status"] == "PENDING":
        deadline = datetime.fromisoformat(row["created_at"]) + timedelta(minutes=PENDING_TTL_MINUTES)
        data["payment_deadline"] = deadline.isoformat(timespec="seconds")
    return data


def mask_phone(phone: str) -> str:
    """'010-1234-5678' → '010-****-5678'. 응답에는 가운데 자리를 가린 번호만 내보낸다."""
    parts = phone.split("-")
    return f"{parts[0]}-{'*' * len(parts[1])}-{parts[2]}" if len(parts) == 3 else phone


def expire_pending_reservations(conn: sqlite3.Connection) -> None:
    """결제 기한이 지난 PENDING 예약을 취소해 시간대를 다시 연다.

    별도 스케줄러 없이, 시간대를 조회하거나 바꾸는 요청마다 먼저 호출한다.
    """
    cutoff = (datetime.now() - timedelta(minutes=PENDING_TTL_MINUTES)).isoformat(timespec="seconds")
    with conn:
        conn.execute(
            "UPDATE reservations SET status = 'CANCELLED' WHERE status = 'PENDING' AND created_at < ?", (cutoff,)
        )


def booked_times(conn: sqlite3.Connection, day: str) -> set[str]:
    rows = conn.execute(
        "SELECT reserve_time FROM reservations WHERE reserve_date = ? AND status != 'CANCELLED'", (day,)
    )
    return {row[0] for row in rows}


def is_slot_open(day: date, slot: str, booked: set[str]) -> bool:
    if slot in booked:
        return False
    if day == date.today() and slot <= datetime.now().strftime("%H:%M"):
        return False
    return True


# ---------------------------------------------------------------------------
# 입력 검증
# ---------------------------------------------------------------------------

def require_text(data: dict, key: str, label: str, max_len: int) -> str:
    value = str(data.get(key) or "").strip()
    if not value:
        raise ApiError(400, f"{label}을(를) 입력해 주세요.")
    if len(value) > max_len:
        raise ApiError(400, f"{label}은(는) {max_len}자 이하로 입력해 주세요.")
    return value


def normalize_phone(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    if not re.fullmatch(r"01[016789]\d{7,8}", digits):
        raise ApiError(400, "연락처 형식이 올바르지 않습니다. (예: 010-1234-5678)")
    return f"{digits[:3]}-{digits[3:-4]}-{digits[-4:]}"


def normalize_car_number(value: str) -> str:
    compact = re.sub(r"\s", "", value)
    if not re.fullmatch(r"\d{2,3}[가-힣]\d{4}", compact):
        raise ApiError(400, "차량번호 형식이 올바르지 않습니다. (예: 12가3456, 123가4567)")
    return compact


def parse_day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ApiError(400, "날짜 형식이 올바르지 않습니다. (YYYY-MM-DD)") from None


def check_reservable_day(day: date) -> None:
    today = date.today()
    if day < today:
        raise ApiError(400, "지난 날짜는 예약할 수 없습니다.")
    if day > today + timedelta(days=MAX_ADVANCE_DAYS):
        raise ApiError(400, f"예약은 오늘부터 {MAX_ADVANCE_DAYS}일 이내의 날짜만 가능합니다.")


# ---------------------------------------------------------------------------
# 파일 업로드
# ---------------------------------------------------------------------------

def parse_multipart(content_type: str, body: bytes) -> tuple[dict[str, str], dict[str, tuple[str, bytes]]]:
    """multipart/form-data 본문을 (일반 필드, 파일 필드)로 분리한다.

    Python 3.13에서 cgi 모듈이 제거되어 경계 문자열 기준으로 직접 파싱한다.
    """
    match = re.search(r'boundary="?([^";]+)"?', content_type)
    if not content_type.startswith("multipart/form-data") or not match:
        raise ApiError(400, "multipart/form-data 형식으로 전송해 주세요.")
    delimiter = b"--" + match.group(1).encode("latin-1")

    fields: dict[str, str] = {}
    files: dict[str, tuple[str, bytes]] = {}
    for chunk in body.split(delimiter)[1:]:
        if chunk.startswith(b"--"):  # 종료 경계
            break
        head, sep, content = chunk.partition(b"\r\n\r\n")
        if not sep:
            continue
        content = content.removesuffix(b"\r\n")
        headers = head.decode("utf-8", "replace")
        disposition = re.search(r"^content-disposition:(.*)$", headers, re.I | re.M)
        if not disposition:
            continue
        name = re.search(r'\bname="([^"]*)"', disposition.group(1))
        filename = re.search(r'\bfilename="([^"]*)"', disposition.group(1))
        if not name:
            continue
        if filename:
            files[name.group(1)] = (unquote(filename.group(1)), content)
        else:
            fields[name.group(1)] = content.decode("utf-8", "replace")
    return fields, files


def _compound_stream(data: bytes, stream_name: str) -> bytes:
    """Microsoft Compound File Binary 컨테이너에서 지정한 스트림을 읽는다."""
    signature = bytes.fromhex("d0cf11e0a1b11ae1")
    if not data.startswith(signature) or len(data) < 512:
        raise ApiError(400, "마이클 Excel 97-2003 형식의 .xls 파일을 선택해 주세요.")

    major_version = struct.unpack_from("<H", data, 26)[0]
    sector_size = 1 << struct.unpack_from("<H", data, 30)[0]
    mini_sector_size = 1 << struct.unpack_from("<H", data, 32)[0]
    if major_version not in (3, 4) or sector_size not in (512, 4096) or mini_sector_size != 64:
        raise ApiError(400, "지원하지 않는 Excel .xls 파일 형식입니다.")

    sector_count = len(data) // sector_size - 1
    if sector_count < 1 or len(data) % sector_size:
        raise ApiError(400, "Excel 파일이 손상되었거나 비어 있습니다.")

    def sector(sector_id: int) -> bytes:
        if not 0 <= sector_id < sector_count:
            raise ApiError(400, "Excel 파일의 섹터 정보가 올바르지 않습니다.")
        start = (sector_id + 1) * sector_size
        result = data[start:start + sector_size]
        if len(result) != sector_size:
            raise ApiError(400, "Excel 파일이 손상되었습니다.")
        return result

    difat = [entry for entry in struct.unpack_from("<109I", data, 76) if entry < 0xFFFFFFFA]
    difat_sector = struct.unpack_from("<I", data, 68)[0]
    difat_sector_count = struct.unpack_from("<I", data, 72)[0]
    if difat_sector_count > sector_count:
        raise ApiError(400, "Excel 파일의 DIFAT 정보가 올바르지 않습니다.")
    for _ in range(difat_sector_count):
        if difat_sector >= sector_count or difat_sector in difat:
            raise ApiError(400, "Excel 파일의 FAT 정보가 올바르지 않습니다.")
        entries = struct.unpack(f"<{sector_size // 4}I", sector(difat_sector))
        difat.extend(entry for entry in entries[:-1] if entry < 0xFFFFFFFA)
        difat_sector = entries[-1]

    fat_sector_count = struct.unpack_from("<I", data, 44)[0]
    if fat_sector_count > len(difat) or fat_sector_count > sector_count:
        raise ApiError(400, "Excel 파일의 FAT 정보가 올바르지 않습니다.")
    fat: list[int] = []
    for fat_sector in difat[:fat_sector_count]:
        fat.extend(struct.unpack(f"<{sector_size // 4}I", sector(fat_sector)))

    def read_chain(first_sector: int, allocation: list[int]) -> bytes:
        chunks = []
        seen = set()
        current = first_sector
        while current < 0xFFFFFFFA:
            if current >= len(allocation) or current in seen or len(seen) >= len(allocation):
                raise ApiError(400, "Excel 파일의 스트림 연결 정보가 올바르지 않습니다.")
            seen.add(current)
            if allocation is fat:
                chunks.append(sector(current))
            else:
                chunk = mini_stream[current * mini_sector_size:(current + 1) * mini_sector_size]
                if len(chunk) != mini_sector_size:
                    raise ApiError(400, "Excel 파일의 mini-stream 정보가 올바르지 않습니다.")
                chunks.append(chunk)
            current = allocation[current]
        return b"".join(chunks)

    directory = read_chain(struct.unpack_from("<I", data, 48)[0], fat)
    entries = []
    for offset in range(0, len(directory) - 127, 128):
        entry = directory[offset:offset + 128]
        name_length = struct.unpack_from("<H", entry, 64)[0]
        if name_length < 2 or name_length > 64:
            continue
        name = entry[:name_length - 2].decode("utf-16le", "replace")
        entry_type = entry[66]
        start_sector = struct.unpack_from("<I", entry, 116)[0]
        size = struct.unpack_from("<Q", entry, 120)[0]
        entries.append((name, entry_type, start_sector, size))

    root = next((entry for entry in entries if entry[1] == 5), None)
    workbook = next((entry for entry in entries
                     if entry[0] in (stream_name, "Book") and entry[1] == 2), None)
    if root is None or workbook is None:
        raise ApiError(400, "Excel 통합 문서에서 워크시트를 찾을 수 없습니다.")

    mini_stream = read_chain(root[2], fat)[:root[3]]
    mini_fat_sector = struct.unpack_from("<I", data, 60)[0]
    mini_fat_count = struct.unpack_from("<I", data, 64)[0]
    if mini_fat_count > sector_count:
        raise ApiError(400, "Excel 파일의 mini-FAT 정보가 올바르지 않습니다.")
    mini_fat = []
    if mini_fat_count:
        raw_mini_fat = read_chain(mini_fat_sector, fat)[:mini_fat_count * sector_size]
        if len(raw_mini_fat) < mini_fat_count * sector_size:
            raise ApiError(400, "Excel 파일의 mini-FAT 정보가 올바르지 않습니다.")
        mini_fat = list(struct.unpack(f"<{len(raw_mini_fat) // 4}I", raw_mini_fat))

    mini_cutoff = struct.unpack_from("<I", data, 56)[0]
    if workbook[3] < mini_cutoff:
        if not mini_fat:
            raise ApiError(400, "Excel 파일의 mini-FAT 정보가 없습니다.")
        content = read_chain(workbook[2], mini_fat)[:workbook[3]]
    else:
        content = read_chain(workbook[2], fat)[:workbook[3]]
    if not content:
        raise ApiError(400, "Excel 통합 문서가 비어 있습니다.")
    return content


def _xls_records(workbook: bytes) -> list[tuple[int, int, bytes]]:
    records = []
    offset = 0
    while offset + 4 <= len(workbook):
        record_id, length = struct.unpack_from("<HH", workbook, offset)
        end = offset + 4 + length
        if end > len(workbook):
            raise ApiError(400, "Excel 워크시트 레코드가 손상되었습니다.")
        records.append((offset, record_id, workbook[offset + 4:end]))
        if len(records) > 200_000:
            raise ApiError(400, "Excel 파일에 처리할 수 없을 만큼 많은 셀이 있습니다.")
        offset = end
    if offset != len(workbook):
        raise ApiError(400, "Excel 워크시트 레코드가 손상되었습니다.")
    return records


def _xls_shared_strings(records: list[tuple[int, int, bytes]]) -> list[str]:
    sst_index = next((index for index, record in enumerate(records) if record[1] == 0x00FC), None)
    if sst_index is None:
        return []

    segments = [records[sst_index][2]]
    for _, record_id, payload in records[sst_index + 1:]:
        if record_id != 0x003C:
            break
        segments.append(payload)

    segment_index = 0
    position = 0

    def read_bytes(count: int) -> bytes:
        nonlocal segment_index, position
        chunks = []
        while count:
            if segment_index >= len(segments):
                raise ApiError(400, "Excel 파일의 문자열 정보가 잘렸습니다.")
            available = len(segments[segment_index]) - position
            if available <= 0:
                segment_index += 1
                position = 0
                continue
            take = min(count, available)
            chunks.append(segments[segment_index][position:position + take])
            position += take
            count -= take
        return b"".join(chunks)

    def read_characters(count: int, wide: bool) -> str:
        nonlocal segment_index, position
        chunks = []
        while count:
            if segment_index >= len(segments):
                raise ApiError(400, "Excel 파일의 문자열 정보가 잘렸습니다.")
            available = len(segments[segment_index]) - position
            width = 2 if wide else 1
            if available <= 0:
                segment_index += 1
                position = 0
                if segment_index >= len(segments) or not segments[segment_index]:
                    raise ApiError(400, "Excel 파일의 문자열 연속 정보가 올바르지 않습니다.")
                wide = bool(segments[segment_index][0] & 1)
                position = 1
                continue
            take = min(count, available // width)
            if not take:
                raise ApiError(400, "Excel 파일의 문자열 인코딩이 올바르지 않습니다.")
            chunks.append(segments[segment_index][position:position + take * width])
            position += take * width
            count -= take
        encoding = "utf-16le" if wide else "cp1252"
        try:
            return b"".join(chunks).decode(encoding)
        except UnicodeDecodeError:
            raise ApiError(400, "Excel 파일의 문자열 인코딩이 올바르지 않습니다.") from None

    head = read_bytes(8)
    _, unique_count = struct.unpack("<II", head)
    if unique_count > 100_000:
        raise ApiError(400, "Excel 파일에 처리할 수 없을 만큼 많은 문자열이 있습니다.")

    strings = []
    for _ in range(unique_count):
        header = read_bytes(3)
        char_count, flags = struct.unpack("<HB", header)
        rich_runs = struct.unpack("<H", read_bytes(2))[0] if flags & 0x08 else 0
        extension_size = struct.unpack("<I", read_bytes(4))[0] if flags & 0x04 else 0
        value = read_characters(char_count, bool(flags & 0x01))
        read_bytes(rich_runs * 4 + extension_size)
        strings.append(value)
    return strings


def _xls_rk_value(raw: int) -> int | float:
    if raw & 0x02:
        value = raw >> 2
        if value & (1 << 29):
            value -= 1 << 30
    else:
        value = struct.unpack("<d", struct.pack("<Q", (raw & 0xFFFFFFFC) << 32))[0]
    return value / 100 if raw & 0x01 else value


def parse_michael_xls(data: bytes) -> dict[str, dict[int, dict[int, str | int | float]]]:
    """마이클 Excel 97-2003 보고서의 워크시트 이름과 셀을 읽는다."""
    workbook = _compound_stream(data, "Workbook")
    records = _xls_records(workbook)
    shared_strings = _xls_shared_strings(records)

    sheets = []
    for _, record_id, payload in records:
        if record_id != 0x0085:
            continue
        if len(payload) < 8:
            raise ApiError(400, "Excel 워크시트 이름 정보가 손상되었습니다.")
        sheet_offset = struct.unpack_from("<I", payload)[0]
        char_count, flags = payload[6], payload[7]
        width = 2 if flags & 1 else 1
        name_bytes = payload[8:8 + char_count * width]
        if len(name_bytes) != char_count * width:
            raise ApiError(400, "Excel 워크시트 이름 정보가 손상되었습니다.")
        try:
            name = name_bytes.decode("utf-16le" if width == 2 else "cp1252")
        except UnicodeDecodeError:
            raise ApiError(400, "Excel 워크시트 이름 인코딩이 올바르지 않습니다.") from None
        sheets.append((sheet_offset, name))
    if not sheets:
        raise ApiError(400, "Excel 통합 문서에 워크시트가 없습니다.")

    workbook_sheets = {}
    for sheet_offset, name in sorted(sheets):
        rows: dict[int, dict[int, str | int | float]] = {}
        in_sheet = False
        for offset, record_id, payload in records:
            if offset < sheet_offset:
                continue
            if record_id == 0x000A:
                if in_sheet:
                    break
                continue
            if record_id == 0x0809:
                in_sheet = True
                continue
            if not in_sheet or record_id not in (0x00FD, 0x0203, 0x027E, 0x00BD, 0x0204, 0x0006):
                continue
            if len(payload) < 6:
                raise ApiError(400, "Excel 셀 데이터가 손상되었습니다.")
            row, column = struct.unpack_from("<HH", payload)
            if row >= 65_536 or column >= 256:
                raise ApiError(400, "Excel 셀 위치 정보가 올바르지 않습니다.")
            value: str | int | float | None = None
            if record_id == 0x00FD:
                if len(payload) < 10:
                    raise ApiError(400, "Excel 문자열 셀이 손상되었습니다.")
                string_index = struct.unpack_from("<I", payload, 6)[0]
                if string_index >= len(shared_strings):
                    raise ApiError(400, "Excel 문자열 참조가 올바르지 않습니다.")
                value = shared_strings[string_index]
            elif record_id == 0x0203:
                if len(payload) < 14:
                    raise ApiError(400, "Excel 숫자 셀이 손상되었습니다.")
                value = struct.unpack_from("<d", payload, 6)[0]
            elif record_id == 0x027E:
                if len(payload) < 10:
                    raise ApiError(400, "Excel 숫자 셀이 손상되었습니다.")
                value = _xls_rk_value(struct.unpack_from("<I", payload, 6)[0])
            elif record_id == 0x00BD:
                if len(payload) < 10:
                    raise ApiError(400, "Excel 숫자 셀이 손상되었습니다.")
                last_column = struct.unpack_from("<H", payload, len(payload) - 2)[0]
                expected_length = 6 + (last_column - column + 1) * 6
                if last_column < column or len(payload) != expected_length:
                    raise ApiError(400, "Excel 숫자 셀이 손상되었습니다.")
                for current_column in range(column, last_column + 1):
                    rk_offset = 4 + (current_column - column) * 6
                    rk = struct.unpack_from("<I", payload, rk_offset + 2)[0]
                    rows.setdefault(row, {})[current_column] = _xls_rk_value(rk)
                continue
            elif record_id == 0x0204:
                if len(payload) < 9:
                    raise ApiError(400, "Excel 문자열 셀이 손상되었습니다.")
                char_count, flags = struct.unpack_from("<HB", payload, 6)
                width = 2 if flags & 1 else 1
                value_bytes = payload[9:9 + char_count * width]
                if len(value_bytes) != char_count * width:
                    raise ApiError(400, "Excel 문자열 셀이 손상되었습니다.")
                try:
                    value = value_bytes.decode("utf-16le" if width == 2 else "cp1252")
                except UnicodeDecodeError:
                    raise ApiError(400, "Excel 셀 문자열 인코딩이 올바르지 않습니다.") from None
            elif record_id == 0x0006:
                if len(payload) < 14:
                    raise ApiError(400, "Excel 수식 셀이 손상되었습니다.")
                cached_value = payload[6:14]
                if cached_value[6:] == b"\xff\xff":
                    continue
                value = struct.unpack("<d", cached_value)[0]
            if value is not None:
                rows.setdefault(row, {})[column] = value
        if name in workbook_sheets:
            raise ApiError(400, "같은 이름의 워크시트가 중복되어 있습니다.")
        workbook_sheets[name] = rows
    return workbook_sheets


def _sheet_rows(cells: dict[int, dict[int, str | int | float]]) -> list[tuple[int, dict[str, str | int | float]]]:
    if not cells:
        return []
    header_row = min(cells)
    headers = {
        column: str(value).strip()
        for column, value in cells[header_row].items()
        if str(value).strip()
    }
    return [
        (row_index + 1, {headers[column]: value for column, value in row.items() if column in headers})
        for row_index, row in sorted(cells.items())
        if row_index > header_row and any(str(value).strip() for value in row.values())
    ]


def _record_date(value: str | int | float) -> str:
    if isinstance(value, (int, float)):
        if not 1 <= value <= 2_958_465:
            raise ApiError(400, "정비 기록의 날짜 값이 올바르지 않습니다.")
        return (datetime(1899, 12, 30) + timedelta(days=int(value))).date().isoformat()
    text = str(value).strip()
    for pattern in ("%Y.%m.%d", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, pattern).date().isoformat()
        except ValueError:
            continue
    raise ApiError(400, f"정비 기록 날짜를 읽을 수 없습니다: {text[:30]}")


def _record_number(value: str | int | float, label: str, allow_blank: bool = False) -> int | None:
    if isinstance(value, (int, float)):
        number = value
    else:
        text = re.sub(r"[^\d.\-]", "", str(value).strip())
        if not text and allow_blank:
            return None
        if not re.fullmatch(r"-?\d+(?:\.\d+)?", text):
            raise ApiError(400, f"정비 기록의 {label} 값을 읽을 수 없습니다.")
        number = float(text)
    if not math.isfinite(number):
        raise ApiError(400, f"정비 기록의 {label} 값이 올바르지 않습니다.")
    if isinstance(number, float) and not number.is_integer():
        raise ApiError(400, f"정비 기록의 {label}은(는) 정수로 입력되어야 합니다.")
    result = int(number)
    if abs(result) > 2_000_000_000:
        raise ApiError(400, f"정비 기록의 {label} 값이 너무 큽니다.")
    return result


def _row_value(row: dict[str, str | int | float], *names: str) -> str | int | float:
    normalized = {re.sub(r"\s+", "", key): value for key, value in row.items()}
    for name in names:
        if (value := normalized.get(re.sub(r"\s+", "", name))) is not None:
            return value
    return ""


def parse_michael_report(data: bytes) -> tuple[str, str, int, list[dict]]:
    sheets = parse_michael_xls(data)
    report_rows = _sheet_rows(sheets.get("리포트", {}))
    metadata = {}
    if report_rows:
        for row in sheets["리포트"].values():
            for column, value in row.items():
                label = str(value).strip()
                if label in ("차량번호", "차종", "제조사", " 최종 누적주행거리", "최종 누적주행거리"):
                    next_value = row.get(column + 1)
                    if next_value is not None:
                        metadata[label.strip()] = next_value
    raw_car_number = str(metadata.get("차량번호", "")).strip()
    if not raw_car_number:
        raise ApiError(400, "마이클 리포트의 차량번호를 찾을 수 없습니다.")
    car_number = normalize_car_number(raw_car_number)
    model_parts = [str(metadata.get(label, "")).strip() for label in ("제조사", "차종")]
    model = " ".join(part for part in model_parts if part) or "차량 정보 없음"
    if len(model) > 40:
        model = model[:40]
    raw_mileage = metadata.get("최종 누적주행거리", 0)
    mileage = _record_number(raw_mileage, "주행거리", allow_blank=True) or 0
    if not 0 <= mileage <= 2_000_000:
        raise ApiError(400, "마이클 리포트의 최종 주행거리 값이 올바르지 않습니다.")

    imported_records = []
    column_specs = {
        "정비": ("항목", "금액(￦)", "누적주행거리(km)", "장소명", "메모"),
        "기타": ("항목", "금액(￦)", "누적주행거리(km)", "장소명", "메모"),
    }
    for sheet_name in ("정비", "기타"):
        for row_number, row in _sheet_rows(sheets.get(sheet_name, {})):
            detail_key, amount_key, mileage_key, place_key, memo_key = column_specs[sheet_name]
            detail = str(_row_value(row, detail_key)).strip()
            if not detail:
                continue
            raw_date = _row_value(row, "날짜")
            if not raw_date:
                raise ApiError(400, f"{sheet_name} 시트 {row_number}행에 날짜가 없습니다.")
            amount = _record_number(_row_value(row, amount_key), "금액") or 0
            record_mileage = _record_number(_row_value(row, mileage_key), "주행거리", allow_blank=True)
            imported_records.append({
                "record_date": _record_date(raw_date),
                "record_type": sheet_name,
                "detail": detail[:200],
                "amount": amount,
                "mileage": record_mileage if record_mileage and record_mileage > 0 else None,
                "place": str(_row_value(row, place_key)).strip()[:200],
                "memo": str(_row_value(row, memo_key)).strip()[:2000],
            })

    for row_number, row in _sheet_rows(sheets.get("주유", {})):
        fuel_type = str(_row_value(row, "구분")).strip()
        if not fuel_type:
            continue
        raw_date = _row_value(row, "날짜")
        if not raw_date:
            raise ApiError(400, f"주유 시트 {row_number}행에 날짜가 없습니다.")
        amount = _record_number(_row_value(row, "금액(￦)"), "금액") or 0
        record_mileage = _record_number(_row_value(row, "누적주행거리(km)"), "주행거리", allow_blank=True)
        notes = [str(_row_value(row, "메모")).strip()]
        for label, suffix in (("주유량(L)", "L"), ("단가(￦/L)", "원/L"),
                              ("구간거리(km)", "km 주행"), ("구간연비(km/L)", "km/L")):
            value = _row_value(row, label)
            if str(value).strip():
                notes.append(f"{label.split('(')[0]} {value}{suffix}")
        imported_records.append({
            "record_date": _record_date(raw_date),
            "record_type": "주유",
            "detail": fuel_type[:200],
            "amount": amount,
            "mileage": record_mileage if record_mileage and record_mileage > 0 else None,
            "place": str(_row_value(row, "장소명")).strip()[:200],
            "memo": " · ".join(note for note in notes if note)[:2000],
        })

    for row_number, row in _sheet_rows(sheets.get("주행", {})):
        raw_date = _row_value(row, "날짜")
        raw_mileage = _row_value(row, "누적주행거리(km)")
        if not raw_date and not raw_mileage:
            continue
        if not raw_date:
            raise ApiError(400, f"주행 시트 {row_number}행에 날짜가 없습니다.")
        record_mileage = _record_number(raw_mileage, "주행거리", allow_blank=True)
        distance = _row_value(row, "구간거리(km)")
        memo = str(_row_value(row, "메모")).strip()
        if str(distance).strip():
            memo = " · ".join(part for part in (memo, f"주행거리 {distance}km") if part)
        imported_records.append({
            "record_date": _record_date(raw_date),
            "record_type": "주행",
            "detail": str(_row_value(row, "구분")).strip() or "주행 기록",
            "amount": 0,
            "mileage": record_mileage if record_mileage and record_mileage > 0 else None,
            "place": "",
            "memo": memo[:2000],
        })

    if not imported_records:
        raise ApiError(400, "마이클 리포트에서 가져올 기록을 찾지 못했습니다.")

    duplicate_counts: dict[str, int] = {}
    for record in imported_records:
        identity = [car_number, *(record[key] for key in
                     ("record_date", "record_type", "detail", "amount", "mileage", "place", "memo"))]
        identity_text = json.dumps(identity, ensure_ascii=False, separators=(",", ":"))
        occurrence = duplicate_counts.get(identity_text, 0)
        duplicate_counts[identity_text] = occurrence + 1
        record["source_key"] = hashlib.sha256(f"{identity_text}:{occurrence}".encode("utf-8")).hexdigest()

    return car_number, model, mileage, imported_records


def looks_like_image(data: bytes, ext: str) -> bool:
    """확장자만 바꾼 파일을 막기 위해 파일 시그니처(매직 넘버)를 확인한다."""
    if ext in (".jpg", ".jpeg"):
        return data.startswith(b"\xff\xd8\xff")
    if ext == ".png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if ext == ".gif":
        return data.startswith((b"GIF87a", b"GIF89a"))
    if ext == ".webp":
        return data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    return False


def _candidate_names(stem: str, ext: str):
    yield f"{stem}{ext}"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    yield f"{stem}_{stamp}{ext}"
    for n in itertools.count(1):  # 같은 초에 같은 이름이 또 올라온 경우
        yield f"{stem}_{stamp}_{n}{ext}"


def save_upload(original_name: str, data: bytes) -> str:
    """검증을 통과한 이미지를 uploads/에 저장하고 저장된 파일명을 반환한다."""
    name = Path(original_name.replace("\\", "/")).name
    ext = Path(name).suffix.lower()
    # 'virus.exe.png'처럼 중간에 숨긴 확장자까지 검사한다.
    if any(suffix.lower() in DANGEROUS_EXTENSIONS for suffix in Path(name).suffixes):
        raise ApiError(400, "위험한 확장자입니다. 실행 파일이나 스크립트는 업로드할 수 없습니다.")
    if ext not in ALLOWED_EXTENSIONS:
        raise ApiError(400, f"허용되지 않는 파일 형식입니다. 허용 확장자: {', '.join(ALLOWED_EXTENSIONS)}")
    if not data:
        raise ApiError(400, "빈 파일은 업로드할 수 없습니다.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ApiError(413, "파일 크기는 최대 5MB까지 업로드할 수 있습니다.")
    if not looks_like_image(data, ext):
        raise ApiError(400, "파일 내용이 이미지 형식과 일치하지 않습니다.")

    stem = re.sub(r"[^\w\-]", "_", Path(name).stem).strip("._")[:60] or "photo"
    if stem.upper() in WINDOWS_RESERVED_NAMES:
        stem = f"file_{stem}"

    # 'xb' 모드는 파일이 이미 있으면 실패하므로, 동시 업로드 상황에서도 기존 파일을 덮어쓰지 않는다.
    for candidate in _candidate_names(stem, ext):
        try:
            with open(UPLOAD_DIR / candidate, "xb") as fp:
                fp.write(data)
            return candidate
        except FileExistsError:
            continue
    raise AssertionError("unreachable")


# ---------------------------------------------------------------------------
# HTTP 핸들러
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "CarMaintenance/1.0"
    timeout = REQUEST_TIMEOUT_SECONDS

    routes = [
        ("GET", r"/api/config", "api_get_config"),
        ("GET", r"/api/items", "api_list_items"),
        ("GET", r"/api/slots", "api_list_slots"),
        ("GET", r"/api/reservations", "api_list_reservations"),
        ("POST", r"/api/reservations", "api_create_reservation"),
        ("POST", r"/api/reservations/(\d+)/pay", "api_pay_reservation"),
        ("POST", r"/api/reservations/(\d+)/cancel", "api_cancel_reservation"),
        ("GET", r"/api/vehicles", "api_list_vehicles"),
        ("POST", r"/api/vehicles", "api_save_vehicle"),
        ("GET", r"/api/records", "api_list_records"),
        ("POST", r"/api/import/michael", "api_import_michael"),
    ]
    compiled_routes = [(method, re.compile(pattern), name) for method, pattern, name in routes]

    def do_GET(self) -> None:
        self.dispatch("GET")

    def do_POST(self) -> None:
        self.dispatch("POST")

    def dispatch(self, method: str) -> None:
        url = urlsplit(self.path)
        self.query = parse_qs(url.query)
        try:
            if method == "POST":
                self.check_same_origin()
            for route_method, pattern, handler_name in self.compiled_routes:
                if route_method == method and (match := pattern.fullmatch(url.path)):
                    return getattr(self, handler_name)(*match.groups())
            if method == "GET" and not url.path.startswith("/api/"):
                return self.serve_static(unquote(url.path))
            raise ApiError(404 if method == "GET" else 405, "요청한 API를 찾을 수 없습니다.")
        except ApiError as exc:
            self.send_json({"error": exc.message}, exc.status)
        except Exception:
            traceback.print_exc()
            self.send_json({"error": "서버 내부 오류가 발생했습니다."}, 500)

    # --- 응답 도우미 -------------------------------------------------------

    def send_json(self, payload: object, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def serve_static(self, path: str) -> None:
        if path in ("/", "/index.html"):
            base, relative = STATIC_DIR, "index.html"
        elif path.startswith("/static/"):
            base, relative = STATIC_DIR, path.removeprefix("/static/")
        elif path.startswith("/uploads/"):
            base, relative = UPLOAD_DIR, path.removeprefix("/uploads/")
        else:
            raise ApiError(404, "페이지를 찾을 수 없습니다.")

        target = (base / relative).resolve()
        if not target.is_relative_to(base.resolve()) or not target.is_file():  # 경로 조작(../) 차단
            raise ApiError(404, "파일을 찾을 수 없습니다.")

        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        if base is UPLOAD_DIR:
            self.send_header("Content-Security-Policy", "default-src 'none'")
        else:
            self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    # --- 요청 검증 ---------------------------------------------------------

    def check_same_origin(self) -> None:
        """CSRF 방어: 다른 사이트의 페이지가 브라우저를 통해 보낸 POST를 거부한다.

        최신 브라우저는 Sec-Fetch-Site를, 그 외에는 Origin을 보낸다.
        둘 다 없는 요청(curl, 스크립트)은 브라우저 요청이 아니므로 통과시킨다.
        """
        site = self.headers.get("Sec-Fetch-Site")
        origin = self.headers.get("Origin")
        if site is not None:
            allowed = site in ("same-origin", "none")
        elif origin is not None:
            allowed = urlsplit(origin).netloc == self.headers.get("Host", "")
        else:
            allowed = True
        if not allowed:
            self.close_connection = True
            raise ApiError(403, "다른 사이트에서 보낸 요청은 처리할 수 없습니다.")

    # --- 요청 본문 ---------------------------------------------------------

    def read_body(self, limit: int, too_large_message: str) -> bytes:
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            raise ApiError(411, "Content-Length 헤더가 필요합니다.") from None
        if length < 0:
            raise ApiError(400, "잘못된 Content-Length 입니다.")
        if length > limit:
            self.close_connection = True  # 본문을 읽지 않고 연결을 끊는다.
            raise ApiError(413, too_large_message)
        return self.rfile.read(length)

    def read_json(self) -> dict:
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            raise ApiError(415, "application/json 형식으로 전송해 주세요.")
        body = self.read_body(MAX_JSON_BYTES, "요청 데이터가 너무 큽니다.")
        try:
            data = json.loads(body or b"{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ApiError(400, "JSON 형식이 올바르지 않습니다.") from None
        if not isinstance(data, dict):
            raise ApiError(400, "JSON 객체 형식으로 전송해 주세요.")
        return data

    # --- 설정 ------------------------------------------------------------

    def api_get_config(self) -> None:
        """화면에서도 같은 기준으로 미리 검사할 수 있도록 서버 설정값을 내려준다."""
        self.send_json({
            "max_upload_bytes": MAX_UPLOAD_BYTES,
            "max_import_bytes": MAX_IMPORT_BYTES,
            "allowed_extensions": list(ALLOWED_EXTENSIONS),
            "dangerous_extensions": list(DANGEROUS_EXTENSIONS),
            "max_advance_days": MAX_ADVANCE_DAYS,
            "pending_ttl_minutes": PENDING_TTL_MINUTES,
        })

    # --- 정비 항목 ---------------------------------------------------------

    def api_list_items(self) -> None:
        with closing(connect()) as conn:
            rows = conn.execute("SELECT * FROM maintenance_items ORDER BY id").fetchall()
        self.send_json({"items": [dict(row) for row in rows]})

    # --- 예약 --------------------------------------------------------------

    def api_list_slots(self) -> None:
        day = parse_day((self.query.get("date") or [""])[0])
        check_reservable_day(day)
        with closing(connect()) as conn:
            expire_pending_reservations(conn)
            booked = booked_times(conn, day.isoformat())
        self.send_json({
            "date": day.isoformat(),
            "slots": [{"time": slot, "available": is_slot_open(day, slot, booked)} for slot in TIME_SLOTS],
        })

    def api_list_reservations(self) -> None:
        with closing(connect()) as conn:
            expire_pending_reservations(conn)
            self.send_json({"reservations": fetch_reservations(conn)})

    def api_create_reservation(self) -> None:
        data = self.read_json()
        customer_name = require_text(data, "customer_name", "예약자 이름", 20)
        phone = normalize_phone(require_text(data, "phone", "연락처", 20))
        car_number = normalize_car_number(require_text(data, "car_number", "차량번호", 20))
        memo = str(data.get("memo") or "").strip()[:500]

        day = parse_day(data.get("reserve_date"))
        check_reservable_day(day)
        reserve_time = str(data.get("reserve_time") or "")
        if reserve_time not in TIME_SLOTS:
            raise ApiError(400, "예약 시간을 선택해 주세요.")

        raw_ids = data.get("item_ids")
        if not isinstance(raw_ids, list) or not raw_ids:
            raise ApiError(400, "정비 항목을 하나 이상 선택해 주세요.")
        try:
            item_ids = sorted({int(item_id) for item_id in raw_ids})
        except (TypeError, ValueError):
            raise ApiError(400, "정비 항목 값이 올바르지 않습니다.") from None

        with closing(connect()) as conn:
            expire_pending_reservations(conn)
            if not is_slot_open(day, reserve_time, booked_times(conn, day.isoformat())):
                raise ApiError(409, "이미 예약되었거나 지난 시간대입니다. 다른 시간을 선택해 주세요.")

            items = conn.execute(
                f"SELECT id, price FROM maintenance_items WHERE id IN ({','.join('?' * len(item_ids))})",
                item_ids,
            ).fetchall()
            if len(items) != len(item_ids):
                raise ApiError(400, "존재하지 않는 정비 항목이 포함되어 있습니다.")
            total = sum(item["price"] for item in items)

            try:
                with conn:
                    cursor = conn.execute(
                        """INSERT INTO reservations
                               (customer_name, phone, car_number, reserve_date, reserve_time,
                                memo, total_price, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (customer_name, phone, car_number, day.isoformat(), reserve_time,
                         memo, total, now_iso()),
                    )
                    conn.executemany(
                        "INSERT INTO reservation_items (reservation_id, item_id, price) VALUES (?, ?, ?)",
                        [(cursor.lastrowid, item["id"], item["price"]) for item in items],
                    )
            except sqlite3.IntegrityError:  # 동시에 같은 시간대를 예약한 경우(유니크 인덱스)
                raise ApiError(409, "이미 예약된 시간대입니다. 다른 시간을 선택해 주세요.") from None

            reservation = fetch_reservations(conn, cursor.lastrowid)[0]
        self.send_json({"reservation": reservation}, 201)

    def api_pay_reservation(self, reservation_id: str) -> None:
        """결제 시뮬레이션: 실제 결제 없이 입력값을 검증하고 승인 결과만 기록한다."""
        data = self.read_json()
        rid = int(reservation_id)

        with closing(connect()) as conn:
            expire_pending_reservations(conn)
            row = conn.execute("SELECT status FROM reservations WHERE id = ?", (rid,)).fetchone()
            if row is None:
                raise ApiError(404, "예약을 찾을 수 없습니다.")
            if row["status"] != "PENDING":
                raise ApiError(409, f"결제 대기 상태의 예약만 결제할 수 있습니다. "
                                    f"(결제 기한 {PENDING_TTL_MINUTES}분이 지나면 자동 취소됩니다)")

            method = data.get("method")
            if method == "card":
                digits = re.sub(r"\D", "", str(data.get("card_number") or ""))
                if len(digits) != 16:
                    raise ApiError(400, "카드번호 16자리를 입력해 주세요.")
                expiry = re.fullmatch(r"(0[1-9]|1[0-2])/(\d{2})", str(data.get("expiry") or "").strip())
                if not expiry:
                    raise ApiError(400, "유효기간을 MM/YY 형식으로 입력해 주세요.")
                today = date.today()
                if (2000 + int(expiry.group(2)), int(expiry.group(1))) < (today.year, today.month):
                    raise ApiError(400, "유효기간이 지난 카드입니다.")
                if not re.fullmatch(r"\d{3}", str(data.get("cvc") or "")):
                    raise ApiError(400, "CVC 3자리를 입력해 주세요.")
                if digits.endswith("0000"):
                    raise ApiError(402, "카드사 승인이 거절되었습니다. (시뮬레이션: 0000으로 끝나는 카드번호)")
                method_label = f"신용카드 (**** {digits[-4:]})"  # 카드번호 전체는 저장하지 않는다.
            elif method == "transfer":
                method_label = f"계좌이체 (입금자: {require_text(data, 'depositor', '입금자명', 20)})"
            else:
                raise ApiError(400, "결제 수단을 선택해 주세요.")

            transaction_id = f"SIM-{datetime.now():%Y%m%d%H%M%S}-{secrets.token_hex(3).upper()}"
            with conn:
                updated = conn.execute(
                    """UPDATE reservations
                          SET status = 'PAID', payment_method = ?, transaction_id = ?, paid_at = ?
                        WHERE id = ? AND status = 'PENDING'""",
                    (method_label, transaction_id, now_iso(), rid),
                ).rowcount
            if not updated:
                raise ApiError(409, "이미 처리된 예약입니다.")
            reservation = fetch_reservations(conn, rid)[0]
        self.send_json({"reservation": reservation, "message": "결제가 완료되었습니다."})

    def api_cancel_reservation(self, reservation_id: str) -> None:
        self.read_json()  # 본문은 쓰지 않지만 JSON만 받아야 다른 사이트의 form 전송이 preflight에 막힌다.
        rid = int(reservation_id)
        with closing(connect()) as conn:
            row = conn.execute("SELECT status FROM reservations WHERE id = ?", (rid,)).fetchone()
            if row is None:
                raise ApiError(404, "예약을 찾을 수 없습니다.")
            with conn:
                updated = conn.execute(
                    "UPDATE reservations SET status = 'CANCELLED' WHERE id = ? AND status != 'CANCELLED'", (rid,)
                ).rowcount
            if not updated:
                raise ApiError(409, "이미 취소된 예약입니다.")
            reservation = fetch_reservations(conn, rid)[0]
        message = "예약이 취소되고 결제 금액이 환불 처리되었습니다." if row["status"] == "PAID" else "예약이 취소되었습니다."
        self.send_json({"reservation": reservation, "message": message})

    # --- 차량 프로필 -------------------------------------------------------

    def api_list_vehicles(self) -> None:
        with closing(connect()) as conn:
            rows = conn.execute("SELECT * FROM vehicles ORDER BY updated_at DESC, id DESC").fetchall()
        self.send_json({"vehicles": [dict(row) for row in rows]})

    def api_list_records(self) -> None:
        car_number = (self.query.get("car_number") or [""])[0].strip()
        record_type = (self.query.get("type") or [""])[0].strip()
        clauses = []
        params = []
        if car_number:
            clauses.append("car_number = ?")
            params.append(normalize_car_number(car_number))
        if record_type:
            if record_type not in ("정비", "주유", "기타", "주행"):
                raise ApiError(400, "기록 종류가 올바르지 않습니다.")
            clauses.append("record_type = ?")
            params.append(record_type)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with closing(connect()) as conn:
            rows = conn.execute(
                f"""SELECT id, car_number, record_date, record_type, detail, amount,
                           mileage, place, memo
                      FROM vehicle_records {where}
                     ORDER BY record_date DESC, id DESC""",
                params,
            ).fetchall()
        self.send_json({"records": [dict(row) for row in rows]})

    def api_import_michael(self) -> None:
        body = self.read_body(MAX_IMPORT_BYTES + MULTIPART_OVERHEAD, "가져오기 파일은 최대 10MB까지 업로드할 수 있습니다.")
        _, files = parse_multipart(self.headers.get("Content-Type", ""), body)
        uploaded = files.get("file")
        if not uploaded or not uploaded[0]:
            raise ApiError(400, "마이클에서 내보낸 .xls 파일을 선택해 주세요.")
        filename, file_data = uploaded
        if Path(filename.replace("\\", "/")).suffix.lower() != ".xls":
            raise ApiError(400, "Excel 97-2003 형식의 .xls 파일만 가져올 수 있습니다.")
        if len(file_data) > MAX_IMPORT_BYTES:
            raise ApiError(413, "가져오기 파일은 최대 10MB까지 업로드할 수 있습니다.")

        car_number, model, report_mileage, records = parse_michael_report(file_data)
        latest_record_mileage = max((record["mileage"] or 0 for record in records), default=0)
        mileage = max(report_mileage, latest_record_mileage)
        inserted = 0
        with closing(connect()) as conn:
            existing = conn.execute(
                "SELECT mileage FROM vehicles WHERE car_number = ?", (car_number,)
            ).fetchone()
            if existing is not None:
                mileage = max(mileage, existing["mileage"])
            with conn:
                conn.execute(
                    """INSERT INTO vehicles (car_number, model, mileage, photo, updated_at)
                       VALUES (?, ?, ?, NULL, ?)
                       ON CONFLICT (car_number) DO UPDATE
                          SET model = excluded.model, mileage = excluded.mileage,
                              updated_at = excluded.updated_at""",
                    (car_number, model, mileage, now_iso()),
                )
                for record in records:
                    result = conn.execute(
                        """INSERT OR IGNORE INTO vehicle_records
                               (car_number, record_date, record_type, detail, amount,
                                mileage, place, memo, source_key, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (car_number, record["record_date"], record["record_type"], record["detail"],
                         record["amount"], record["mileage"], record["place"], record["memo"],
                         record["source_key"], now_iso()),
                    )
                    inserted += result.rowcount
            vehicle = dict(conn.execute(
                "SELECT * FROM vehicles WHERE car_number = ?", (car_number,)
            ).fetchone())
        self.send_json({
            "vehicle": vehicle,
            "imported_count": inserted,
            "duplicate_count": len(records) - inserted,
            "total_count": len(records),
        })

    def api_save_vehicle(self) -> None:
        """차량번호 기준으로 프로필을 등록하거나 갱신한다(사진 포함)."""
        body = self.read_body(MAX_UPLOAD_BYTES + MULTIPART_OVERHEAD, "파일 크기는 최대 5MB까지 업로드할 수 있습니다.")
        fields, files = parse_multipart(self.headers.get("Content-Type", ""), body)

        car_number = normalize_car_number(require_text(fields, "car_number", "차량번호", 20))
        model = require_text(fields, "model", "차종", 40)
        try:
            mileage = int(fields.get("mileage") or 0)
        except ValueError:
            raise ApiError(400, "주행거리는 숫자로 입력해 주세요.") from None
        if not 0 <= mileage <= 2_000_000:
            raise ApiError(400, "주행거리 값이 올바르지 않습니다.")

        photo = files.get("photo")
        has_photo = bool(photo and photo[0])  # 파일 미선택 시 브라우저는 빈 파일명을 보낸다.

        with closing(connect()) as conn:
            existing = conn.execute("SELECT photo FROM vehicles WHERE car_number = ?", (car_number,)).fetchone()
            if existing is None and not has_photo:
                raise ApiError(400, "차량 사진을 선택해 주세요.")
            stored_photo = save_upload(*photo) if has_photo else existing["photo"]

            try:
                with conn:
                    conn.execute(
                        """INSERT INTO vehicles (car_number, model, mileage, photo, updated_at)
                           VALUES (?, ?, ?, ?, ?)
                           ON CONFLICT (car_number) DO UPDATE
                              SET model = excluded.model, mileage = excluded.mileage,
                                  photo = excluded.photo, updated_at = excluded.updated_at""",
                        (car_number, model, mileage, stored_photo, now_iso()),
                    )
            except Exception:
                if has_photo:  # DB에 기록되지 않은 파일은 남기지 않는다.
                    (UPLOAD_DIR / stored_photo).unlink(missing_ok=True)
                raise

            # 사진을 교체했으면 더 이상 참조되지 않는 이전 파일을 지운다.
            if has_photo and existing is not None and existing["photo"] and existing["photo"] != stored_photo:
                (UPLOAD_DIR / existing["photo"]).unlink(missing_ok=True)
            vehicle = dict(conn.execute("SELECT * FROM vehicles WHERE car_number = ?", (car_number,)).fetchone())
        self.send_json({"vehicle": vehicle, "created": existing is None}, 201 if existing is None else 200)


def main() -> None:
    parser = argparse.ArgumentParser(description="자동차 정비 관리 웹 서버")
    parser.add_argument("--host", default="127.0.0.1", help="바인딩 주소 (기본값: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="포트 번호 (기본값: 8000)")
    args = parser.parse_args()

    init_storage()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"자동차 정비 관리 서버 실행 중: http://{args.host}:{args.port}  (종료: Ctrl+C)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n서버를 종료합니다.")
    finally:
        server.server_close()


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # Windows 콘솔 한글 출력
    main()
