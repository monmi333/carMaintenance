# carMaintenance 코드 분석

> 분석일: 2026-10-27 · 대상: `server.py`, `static/index.html`, `static/app.js`, `static/style.css`
> 함수 위치는 줄 번호 대신 함수명으로 표기합니다. 이름으로 검색하면 바로 찾을 수 있습니다.

---

## 1. 프로젝트 구조

```
N:\carMaintenance\
├── server.py            ★ 백엔드 진입점: HTTP 서버 + API + DB + 업로드
├── static\              프론트엔드 (빌드 없이 순수 HTML/CSS/JS)
│   ├── index.html       ★ 화면 진입점 (단일 페이지)
│   ├── app.js           화면 동작, API 호출
│   └── style.css        디자인
├── uploads\             업로드된 차량 사진 저장소
├── data\                SQLite DB (첫 실행 때 car_maintenance.db 생성)
├── README.md            실행 방법, 테스트 시나리오, API 목록
├── CLAUDE.md            Claude Code용 개발 안내
├── FileAnalyze.md       이 문서
└── 사용한명령어.md        수업용 자연어 명령어 기록 (코드와 무관)
```

### 주요 진입점

| 구분 | 파일 | 시작 지점 |
|---|---|---|
| 백엔드 | `server.py` | `if __name__ == "__main__":` → `main()` |
| 프론트엔드 | `static/index.html` | 맨 아래에서 `app.js`를 불러오고, `app.js` 마지막 줄 `showTab(...)`이 화면을 시작 |

### 파일별 역할

- **`server.py`**: Python 표준 라이브러리만 사용하는 백엔드 전체입니다. 설정 상수, DB, 입력 검증, 업로드, HTTP 처리로 나뉘어 있습니다.
  - DB 테이블: `maintenance_items`(정비 항목), `reservations`(예약), `reservation_items`(예약별 항목과 예약 당시 가격), `vehicles`(차량 프로필)
- **`static/index.html`**: 탭 4개(정비 항목, 정비 예약, 예약 내역, 차량 프로필)와 결제 창의 화면 뼈대입니다.
- **`static/app.js`**: 주소 끝의 `#`으로 탭을 전환하고, 각 탭의 데이터를 불러와 그립니다. 업로드 전에 화면에서도 확장자와 용량을 검사합니다.
- **`static/style.css`**: 색상 변수, 카드형 레이아웃, 좁은 화면 대응을 담당합니다.
- **`data/`, `uploads/`**: 서버를 재시작해도 남는 데이터가 저장되는 곳입니다. DB의 `vehicles.photo`에는 업로드된 파일의 이름만 저장됩니다.

---

## 2. 핵심 함수와 클래스

### 2-1. 백엔드 요청 처리 구조

```
브라우저 요청
   ▼
Handler.do_GET / do_POST
   ▼
Handler.dispatch(method) ─── routes 표에서 경로를 정규식으로 찾음
   │            └─ /api/가 아닌 GET 요청 → serve_static()
   ▼
api_* 메서드 ─── read_json() / read_body()로 요청 내용 읽기
   │             ├─ 검증 함수 (require_text, normalize_*)
   │             └─ DB 함수 (connect, fetch_reservations ...)
   ▼
send_json(payload, status)  ← 정상 결과
   또는 ApiError → dispatch가 잡아서 {"error": 메시지}로 응답
```

### 2-2. 클래스

| 클래스 | 역할 | 입력 | 출력 |
|---|---|---|---|
| `ApiError(Exception)` | 사용자에게 보여줄 오류를 담는 예외 | `status`, `message` | `{"error": message}` + 해당 상태 코드 |
| `Handler(BaseHTTPRequestHandler)` | HTTP 요청 하나를 처리. 요청마다 새로 생성 | `self.path`, `self.headers`, `self.rfile` | `self.wfile`로 응답 |

### 2-3. 요청·응답 도우미 (`Handler` 메서드)

| 함수 | 역할 | 입력 → 출력 |
|---|---|---|
| `dispatch(method)` | 경로에 맞는 `api_*` 메서드를 실행하고 오류를 JSON으로 바꿈 | → 응답. 예상하지 못한 예외는 500 |
| `read_body(limit, msg)` | `Content-Length`를 먼저 확인하고 본문을 읽음. 제한을 넘으면 본문을 읽기 전에 413 | → `bytes` |
| `read_json()` | Content-Type이 JSON인지, 내용이 객체(dict)인지 검사 | → `dict` (실패하면 400/415) |
| `send_json(payload, status)` | JSON 응답과 보안 헤더 전송 | `dict` → 응답 |
| `serve_static(path)` | `/`, `/static/*`, `/uploads/*` 파일 제공. `resolve()`와 `is_relative_to()`로 `../` 경로 조작 차단 | → 파일 내용 |

### 2-4. 입력 검증 함수 (정리된 값을 돌려주거나 `ApiError` 발생)

| 함수 | 입력 예 | 출력 예 |
|---|---|---|
| `require_text(data, key, label, max_len)` | dict, 키 | 앞뒤 공백 제거한 문자열 (비었거나 너무 길면 400) |
| `normalize_phone(value)` | `"01012345678"` | `"010-1234-5678"` |
| `normalize_car_number(value)` | `"12가 3456"` | `"12가3456"` |
| `parse_day(value)` | `"2026-12-01"` | `date` 객체 |

> 예약과 차량 프로필은 **차량번호 문자열로 연결**합니다. 그래서 차량번호를 항상 같은 형태로 정리해서 저장해야 합니다.

### 2-5. DB 함수

| 함수 | 역할 | 출력 |
|---|---|---|
| `connect()` | 연결 생성. 결과를 dict처럼 읽게 설정하고 외래키 검사를 켬 | `Connection` |
| `init_storage()` | 폴더 생성, `SCHEMA` 적용, 정비 항목이 없으면 `SEED_ITEMS` 입력 | 없음 |
| `fetch_reservations(conn, id=None)` | 예약 + 차량 사진(LEFT JOIN) + 예약별 항목(한 번의 추가 조회로 묶음) | `[{..., "items": [...], "vehicle_photo": ...}]` |
| `booked_times(conn, day)` | 해당 날짜에 취소되지 않은 예약의 시간 목록 | `{"10:00", ...}` |
| `is_slot_open(day, slot, booked)` | 이미 예약됐거나 오늘 이미 지난 시간이면 예약 불가 | `bool` |

### 2-6. 업로드 함수

```
요청 본문(bytes)
  └─ parse_multipart(content_type, body)
        → fields {"car_number", "model", "mileage"}
        → files  {"photo": ("내 차.png", b"\x89PNG...")}
  └─ save_upload(파일명, 데이터)
        ① 확장자 허용 목록 (.jpg .jpeg .png .gif .webp)   → 400
        ② 빈 파일 / 5MB 초과                             → 400 / 413
        ③ looks_like_image(): 파일 앞부분으로 실제 이미지인지 확인 → 400
        ④ 파일명 정리 ("내 차" → "내_차", Windows 예약어 처리)
        ⑤ _candidate_names()의 후보를 차례로 시도
             "내_차.png" → "내_차_20261027_221434.png" → "..._1.png"
           open(..., "xb")로 저장: 이미 있는 이름이면 다음 후보로 넘어가므로 덮어쓰지 않음
        → 저장된 파일명
```

`parse_multipart`는 직접 구현했습니다. Python 3.13에서 `cgi` 모듈이 제거되었기 때문입니다.

### 2-7. API 메서드

| 메서드 | 경로 | 입력 | 처리 | 출력 |
|---|---|---|---|---|
| `api_list_items` | `GET /api/items` | 없음 | 정비 항목 전체 조회 | `{"items"}` |
| `api_list_slots` | `GET /api/slots?date=` | 날짜 | `booked_times` + `is_slot_open` | `{"slots": [{"time", "available"}]}` |
| `api_list_reservations` | `GET /api/reservations` | 없음 | `fetch_reservations` | `{"reservations"}` |
| `api_create_reservation` | `POST /api/reservations` | JSON | 검증 → 시간대 확인 → 가격 합계 → 트랜잭션으로 INSERT(`PENDING`) | 201. 중복이면 409 |
| `api_pay_reservation` | `POST …/{id}/pay` | JSON | 상태 확인 → 카드·계좌 검증 → 승인번호 생성 → 조건부 UPDATE | 200. 402/409 가능 |
| `api_cancel_reservation` | `POST …/{id}/cancel` | 없음 | 조건부 UPDATE(`CANCELLED`) | 결제된 예약이었으면 "환불" 메시지 |
| `api_list_vehicles` | `GET /api/vehicles` | 없음 | 최근 수정 순 조회 | `{"vehicles"}` |
| `api_save_vehicle` | `POST /api/vehicles` | multipart | 검증 → `save_upload` → 차량번호 기준 등록 또는 수정 | 201(신규) / 200(수정) |

동시에 같은 요청이 들어와도 안전한 이유:
- **같은 시간대 중복 예약**: 앱에서 먼저 확인하지만 거의 동시에 들어온 요청은 둘 다 통과할 수 있습니다. 이때는 DB의 부분 유니크 인덱스 `ux_reservations_slot`이 하나를 막고, 그 오류(`IntegrityError`)를 409로 바꿔 응답합니다.
- **이중 결제·이중 취소**: 상태를 바꾸는 UPDATE에 현재 상태 조건(`WHERE status = ...`)을 붙입니다. 실제로 바뀐 행이 0개(`rowcount == 0`)이면 이미 처리된 것으로 보고 409를 돌려줍니다.

### 2-8. 프론트엔드 (`static/app.js`)

| 함수 | 역할 |
|---|---|
| `h(tag, attrs, ...children)` | 화면 요소 생성. 글자를 텍스트로만 넣어 XSS 방지 |
| `api(path, options)` / `postJson` | `fetch`로 서버 호출. 실패하면 서버가 보낸 `error` 메시지로 예외 발생 |
| `toast(msg, type)` | 화면 하단 알림 |
| `showTab(name)` | 탭 전환, 주소 끝 `#` 갱신, `tabLoaders[name]()` 실행 |
| `loadItems` → `renderItemsTable`, `renderReserveItems` | 정비 항목 조회 → `state.items` → 표, 예약 체크박스 |
| `loadSlots(day)` | 날짜별 예약 가능 시간 버튼 |
| `selectedItems`, `updateSummary` | 선택한 항목과 합계 |
| `openPayment(r)` + 결제 폼 `submit` | 결제 창 → `/pay` → 결과 화면 또는 오류 |
| `loadHistory` → `renderReservation(r)`, `cancelReservation(r)` | 예약 내역 카드, 결제·취소 |
| `validatePhoto`, `setPreview` | 업로드 전 검사, 미리보기 |
| `loadVehicles`, `editVehicle(v)` | 차량 목록, 카드를 누르면 수정 폼 채우기 |

`state`는 화면 전체가 함께 쓰는 값으로, `items`(정비 항목 캐시)와 `payingReservation`(지금 결제 중인 예약)을 담습니다.

---

## 3. 전체 실행 흐름

### 단계 0. 서버 시작: `python server.py`

```
<module>
 ├─ sys.stdout.reconfigure(encoding="utf-8")
 └─ main()
     ├─ argparse → host=127.0.0.1, port=8000
     ├─ init_storage()
     │   ├─ DATA_DIR / UPLOAD_DIR 생성
     │   ├─ connect() → executescript(SCHEMA)
     │   └─ (정비 항목이 0건이면) executemany(SEED_ITEMS)
     ├─ ThreadingHTTPServer((host, port), Handler)
     └─ serve_forever()          # 요청마다 스레드 1개 + Handler 객체 1개
```

이때 `data/car_maintenance.db` 파일이 생깁니다.

### 단계 1. 첫 화면 로딩

```
GET / → dispatch → serve_static("/") → index.html
  → /static/style.css, /static/app.js 요청 (serve_static)
  → app.js: showTab(location.hash) → loadItems()
      → GET /api/items → api_list_items → SELECT → JSON
      → state.items → renderItemsTable(), renderReserveItems()
```

### 단계 2. 예약 입력과 저장

```
예약일 change → loadSlots → GET /api/slots → 시간 버튼
예약 submit → 화면에서 필수값 검사
  → POST /api/reservations → api_create_reservation
      ├─ read_json → 검증 함수들 (실패 시 400)
      ├─ is_slot_open(booked_times) (실패 시 409)
      ├─ 항목 가격 조회 → 합계
      ├─ with conn: INSERT reservations(PENDING) + reservation_items (실패 시 IntegrityError → 409)
      └─ fetch_reservations → 201
  → openPayment(reservation)
```

이때 `reservations`에 1행, `reservation_items`에 선택한 항목 수만큼 행이 저장됩니다.

### 단계 3. 결제

```
결제 submit → POST /api/reservations/{id}/pay → api_pay_reservation
  ├─ 상태 확인 (404 / 409)
  ├─ 카드: 16자리·유효기간·CVC, 0000으로 끝나면 402 / 계좌: 입금자명
  ├─ 승인번호 "SIM-YYYYMMDDHHMMSS-XXXXXX"
  └─ UPDATE … SET status='PAID' … WHERE id=? AND status='PENDING'
→ 결과 화면 → 창을 닫으면 loadHistory()
```

이때 해당 예약 행의 `status`, `payment_method`(카드는 끝 4자리만), `transaction_id`, `paid_at`이 갱신됩니다.

### 단계 4. 차량 사진 업로드

```
파일 선택 → validatePhoto → setPreview
저장 submit → FormData → POST /api/vehicles → api_save_vehicle
  ├─ read_body(5MB + 64KB) (초과하면 본문을 읽기 전에 413)
  ├─ parse_multipart → 검증
  ├─ save_upload → uploads/파일명 ("xb" 모드, 타임스탬프로 이름 중복 회피)
  └─ INSERT … ON CONFLICT(car_number) DO UPDATE
→ loadVehicles() → <img src="/uploads/파일명"> → serve_static
```

이때 `uploads/`에 이미지 파일이, `vehicles`에 그 파일명이 저장됩니다.

### 단계 5. 재시작 후 데이터 유지

모든 데이터는 DB 파일과 `uploads/` 폴더에 있고, 메모리에 따로 보관하는 값이 없습니다. 다시 시작할 때 `CREATE … IF NOT EXISTS`는 기존 데이터를 건드리지 않고, 기본 데이터도 정비 항목이 0건일 때만 넣습니다.

---

## 4. 개선점과 취약점

이미 잘 처리된 부분은 다음과 같습니다.
- SQL은 모두 파라미터 바인딩을 사용합니다.
- 화면 출력은 `textContent`로 처리해 XSS가 막혀 있습니다.
- `../` 경로 조작을 막습니다.
- 같은 시간대 동시 예약, 이중 결제를 막습니다.
- 카드번호는 끝 4자리만 저장합니다.
- 업로드 응답에 `nosniff`와 CSP 헤더를 붙입니다.

### 🔴 높음: 보안

| # | 문제 | 위치 | 영향 | 개선 방법 |
|---|---|---|---|---|
| 1 | **CSRF** | `api_cancel_reservation`, `api_save_vehicle` | 취소 API는 요청 본문을 읽지 않아 Content-Type 검사가 없습니다. 악성 페이지의 `<form method=post>`만으로 예약이 취소될 수 있습니다. multipart는 preflight 없이 보낼 수 있는 형식이라, 다른 사이트가 차량 정보·사진을 바꾸거나 파일을 계속 올려 디스크를 채울 수 있습니다. (JSON만 받는 API는 preflight가 필요해 막힙니다.) | `dispatch()`에서 모든 POST에 `Origin` / `Sec-Fetch-Site`를 검사하거나 사용자 정의 헤더를 필수로 요구합니다. |
| 2 | **인증·권한 없음 + 순차 ID** | `/api/reservations*` | 전체 예약의 이름과 전화번호가 그대로 보입니다. ID가 1, 2, 3…이라 남의 예약도 결제·취소할 수 있습니다. `--host 0.0.0.0`으로 실행하면 같은 네트워크 전체에 노출됩니다. | 예약 토큰(`secrets.token_urlsafe`) 또는 연락처 확인을 추가합니다. 전화번호를 가리고(`010-****-5678`) 관리자 기능은 분리합니다. |
| 3 | **연결 타임아웃 없음 (Slowloris)** | `Handler` | 요청을 아주 천천히 보내는 클라이언트가 스레드를 무한정 붙잡아 서비스가 멈출 수 있습니다. | `Handler.timeout = 30`을 설정하고 동시 처리 수를 제한합니다. |

### 🟠 중간: 자원·데이터 관리

| # | 문제 | 위치 | 개선 방법 |
|---|---|---|---|
| 4 | 사진을 교체해도 이전 파일이 남고(고아 파일), 업로드 용량·횟수 제한이 없음 | `api_save_vehicle`, `save_upload` | 교체할 때 이전 파일을 정리하고 전체 용량 제한을 둡니다. |
| 5 | 본문 전체를 메모리에 올림 (동시에 5MB 업로드가 많으면 메모리 부담) | `read_body` | 동시 업로드 수를 제한하거나 임시 파일로 나눠 받습니다. |
| 6 | 사진의 EXIF 정보(GPS 위치 등)를 그대로 저장·공개 | `save_upload` | JPEG의 APP1 세그먼트를 제거합니다. |
| 7 | 파일을 먼저 저장하고 DB에 기록하므로, DB 기록이 실패하면 파일만 남음 | `api_save_vehicle` | 실패 시 저장한 파일을 삭제합니다. |
| 8 | SQLite 기본 저널 모드라 동시 쓰기에서 잠금 대기가 생김 | `connect` / `init_storage` | `PRAGMA journal_mode=WAL`을 켭니다. |
| 9 | 매직 넘버는 파일 앞부분만 확인하므로 이미지와 HTML이 섞인 파일(polyglot)도 통과 | `looks_like_image` | 지금은 `nosniff`와 CSP로 실행이 막혀 위험이 낮습니다. 더 엄격하게 하려면 파일 구조까지 검사합니다. |

### 🟡 낮음: 코드 품질·운영

| # | 문제 | 개선 방법 |
|---|---|---|
| 10 | 취소 API가 요청 본문을 읽지 않음. 연결 재사용(keep-alive)을 켜면 남은 본문이 다음 요청에 섞임 (지금은 HTTP/1.0 기본값이라 문제없음) | 모든 POST에서 본문을 읽도록 통일합니다. |
| 11 | 업로드 제한값이 `server.py`와 `app.js`에 중복됨 | `GET /api/config`로 서버 값을 내려줍니다. |
| 12 | `server.py` 한 파일(분석 시점 522줄, 개선 후 603줄)에 모든 기능이 섞여 있음 | `db.py`, `uploads.py`, `validators.py`, `handlers.py`로 나눕니다. |
| 13 | 저장소에 자동 테스트가 없음 | `tests/`에 `unittest`를 추가합니다. 예: 임시 포트 + 임시 DB로 서버를 띄워 테스트 |
| 14 | 예약 목록에 페이지 나눔이 없고, 취소·환불 이력이 남지 않음 | `limit/offset`을 추가하고 상태 변경 이력 테이블을 둡니다. |
| 15 | HTTP 평문 통신이라 카드 입력값이 암호화 없이 전송됨 | 시뮬레이션이라 괜찮지만, 실제 카드번호를 입력하지 말라는 안내를 명시합니다. |
| 16 | `.exe` 등을 올려도 "위험한 확장자"가 아니라 일반적인 형식 오류 메시지가 나옴 | 위험 확장자 목록을 따로 두고 별도 메시지를 보여줍니다. |

### 검토에서 새로 찾은 문제

| # | 문제 | 재현 결과 | 심각도 |
|---|---|---|---|
| A | 결제하지 않은 `PENDING` 예약이 시간대를 계속 차지함 | 결제 없이 하루치 9개 시간대를 **모두 점유**함 | 🔴 높음 |
| B | 먼 미래 날짜도 예약됨 | `9999-12-31` 예약에 201 응답 | 🟠 중간 |
| C | 차량번호만 알면 누구나 남의 차량 프로필을 덮어쓸 수 있음 (#2의 일부) | 차량번호가 같으면 기존 정보가 수정됨 | 🟠 중간 |
| D | 지난 날짜로 시간대를 조회하면 '예약 가능'으로 표시됨 | 수정 중 발견 | 🟡 낮음 |

---

## 5. 개선 반영 결과 (2026-10-27)

### 검증 방법
코드를 임시 폴더 두 곳에 복사해 각각 서버를 띄우고 검증했습니다. 그래서 프로젝트의 `data/`와 `uploads/`는 건드리지 않았습니다.
- 기존 스모크 테스트 **30/30 통과**: 기능이 깨지지 않았는지 확인
- 개선 검증 테스트 **23/23 통과**

### 반영 내역

| 항목 | 수정 내용 | 위치 | 검증 |
|---|---|---|---|
| #1, #10 CSRF | 모든 POST에서 `Sec-Fetch-Site`(없으면 `Origin`)를 검사하고, 다른 사이트에서 보낸 요청은 403. 취소 API도 `read_json()`을 호출해 JSON만 받도록 변경 | `Handler.check_same_origin`, `dispatch`, `api_cancel_reservation` | 외부 출처 취소·차량 등록 → 403, form 형식 취소 → 415, 같은 출처는 정상 |
| A 결제 대기 만료 | 15분이 지난 `PENDING`을 자동 취소. 시간대 조회, 예약 목록·생성, 결제 전에 실행. 응답에 `payment_deadline`을 넣고 화면에 결제 기한 표시 | `expire_pending_reservations`, `serialize_reservation`, `app.js` | 20분 전 예약 → 시간대 다시 열림, 결제 409, 재예약 201 |
| #3 타임아웃 | 소켓 읽기 30초 제한 | `Handler.timeout` | 미완성 요청 연결이 30.0초 뒤 종료 |
| B, D 날짜 범위 | 오늘부터 90일 이내만 허용. 시간대 조회에도 같은 검사를 적용. 화면의 날짜 입력에 `max` 지정 | `check_reservable_day`, `app.js start()` | 9999-12-31·91일 뒤 → 400, 지난 날짜 조회 → 400 |
| #2 일부 | 응답의 전화번호를 `010-****-5678` 형태로 가림 | `mask_phone`, `serialize_reservation` | 목록 전체 가림 확인 |
| #4, #7 업로드 정리 | 사진 교체 시 이전 파일 삭제, DB 기록 실패 시 새 파일 삭제 | `api_save_vehicle` | 교체 후 이전 파일 없음 확인 |
| #8 WAL | `PRAGMA journal_mode = WAL` | `init_storage` | `journal_mode == wal` |
| #11 설정 중복 | `GET /api/config`로 서버 설정을 내려주고, 화면은 시작할 때 받아서 사용 | `api_get_config`, `app.js config` | 응답 값 확인 |
| #16 위험 확장자 | `.exe`, `.bat`, `.ps1` 등은 "위험한 확장자" 메시지로 거부. `photo.exe.png`처럼 중간 확장자도 검사. 화면에서도 같은 메시지 | `DANGEROUS_EXTENSIONS`, `save_upload`, `validatePhoto` | `virus.exe`, `setup.bat`, `photo.exe.png` → 위험 메시지, `doc.txt` → 일반 메시지 |

### 남은 과제 (이번 범위에서 제외)

| 항목 | 이유 |
|---|---|
| #2 인증·권한 (예약 토큰, 관리자 분리), C 차량 덮어쓰기 | 화면 흐름과 데이터 구조를 함께 바꿔야 하는 기능 추가라서 제외 |
| A의 남은 한계 | 결제 기한 15분 동안은 여전히 결제 없이 시간대를 잡아둘 수 있음. 막으려면 연락처당 결제 대기 예약 수 제한이나 요청 횟수 제한(rate limit)이 필요 |
| #5 메모리 사용, #4 전체 업로드 용량 제한 | 동시 요청 수 제한 정책이 먼저 정해져야 함 |
| #6 EXIF 제거, #9 polyglot 정밀 검사 | 지금은 nosniff와 CSP로 위험이 낮음 |
| #12 파일 분리, #13 정식 테스트, #14 페이지 나눔·이력 | 구조 개선 작업 |
| 실제 브라우저 확인 | 서버 응답만 테스트했고, 브라우저 화면을 직접 클릭해 보지는 않음 |
