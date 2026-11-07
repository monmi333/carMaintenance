# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 프로젝트 개요

자동차 정비 관리 웹 애플리케이션입니다. 기능은 정비 항목 조회, 예약과 결제 시뮬레이션, 차량 사진 업로드입니다.
**핵심 제약: Python 표준 라이브러리만 사용합니다.** `pip install`이나 외부 패키지를 추가하지 마세요. 대상 버전은 Python 3.12 이상입니다.
프론트엔드도 빌드 도구나 프레임워크 없이 순수 HTML/CSS/JS로 작성합니다. UI 문구와 오류 메시지는 한국어입니다.

## 명령어

```powershell
python server.py                 # http://127.0.0.1:8000
python server.py --port 8765     # 포트 변경 (--host 0.0.0.0 으로 외부 접속 허용)
python -m py_compile server.py   # 문법 검사
```

macOS에서는 터미널에서 프로젝트 폴더로 이동한 뒤 `python` 대신 `python3`을 사용합니다.

```bash
python3 server.py
python3 server.py --port 8765
python3 -m py_compile server.py
```

빌드, 린트 도구, 저장소 안의 테스트 스위트는 없습니다. 동작 확인은 서버를 띄운 뒤 `urllib`으로 API를 호출해서 합니다. 이때 실제 `data/`와 `uploads/`에 데이터가 쌓입니다.
DB 초기화가 필요하면 서버를 끄고 `data/car_maintenance.db`를 삭제하세요. 다음 실행 때 스키마와 시드 데이터(정비 항목 12개)가 다시 만들어집니다.

## 아키텍처

백엔드 전체가 `server.py` 한 파일에 있습니다.

- **CSRF 방어**: `dispatch()`는 라우팅 전에 모든 POST에 대해 `check_same_origin()`을 실행합니다(`Sec-Fetch-Site`, 없으면 `Origin` 검사). 새 POST API도 JSON 본문을 받게 만드세요. 본문을 쓰지 않는 API라도 `read_json()`을 호출해야 다른 사이트의 form 전송이 preflight에 막힙니다(`api_cancel_reservation` 참고). 스크립트로 POST 테스트할 때는 `Origin`을 아예 보내지 않거나 서버 주소와 같게 보내야 합니다.
- **라우팅**: `Handler.routes`의 `(메서드, 정규식, 메서드명)` 테이블을 `dispatch()`가 `fullmatch`로 매칭합니다. 정규식 그룹은 핸들러의 인자로 전달됩니다. 새 API는 이 테이블에 한 줄을 추가하고 `api_*` 메서드를 구현하면 됩니다. `/api/`가 아닌 GET 요청은 `serve_static()`이 처리합니다(`/` → `static/index.html`, `/static/*`, `/uploads/*`).
- **오류 처리**: 핸들러는 `ApiError(status, 메시지)`를 raise합니다. 그러면 `dispatch()`가 `{"error": 메시지}` JSON으로 응답합니다. 예상하지 못한 예외는 500으로 응답합니다. 프론트엔드의 `api()`는 이 `error` 필드를 그대로 토스트로 보여주므로, 메시지는 사용자에게 보여줄 한국어로 작성합니다.
- **DB**: 요청마다 `with closing(connect()) as conn:`로 연결을 엽니다. 쓰기는 `with conn:` 트랜잭션 안에서 합니다. 스키마는 `SCHEMA` 상수에 있고 `init_storage()`가 `CREATE IF NOT EXISTS`로 적용합니다. 마이그레이션 도구는 없으므로 컬럼을 바꾸면 DB를 삭제해야 합니다.
- **예약 상태 흐름**: `PENDING` → (`/pay`) → `PAID`, 그리고 `PENDING`/`PAID` → (`/cancel`) → `CANCELLED`. 상태를 바꾸는 UPDATE에는 항상 `WHERE status = ...` 조건을 붙이고 `rowcount`로 경쟁 상태를 감지합니다.
- **결제 대기 만료**: `PENDING_TTL_MINUTES`(15분)가 지난 `PENDING`은 `expire_pending_reservations()`가 `CANCELLED`로 바꿉니다. 스케줄러가 없으므로 시간대를 읽거나 바꾸는 API(slots, 예약 목록·생성, 결제)는 DB 작업 전에 반드시 이 함수를 먼저 호출해야 합니다.
- **응답 직렬화**: 예약은 `serialize_reservation()`을 거쳐 나갑니다. 전화번호 가리기(`mask_phone`)와 `payment_deadline` 계산이 여기서 이루어지므로, 예약을 응답할 때는 `fetch_reservations()`를 사용하세요.
- **중복 예약 방지**: 앱 코드의 사전 체크와 별개로, 부분 유니크 인덱스 `ux_reservations_slot`(`WHERE status != 'CANCELLED'`)이 최종 방어선입니다. `IntegrityError`는 409로 바꿔서 응답합니다.
- **가격 스냅샷**: `reservation_items.price`에는 예약 시점의 가격을 저장합니다. 예약 합계는 `maintenance_items`의 현재 가격이 아니라 이 값을 기준으로 합니다.
- **차량 ↔ 예약 연결**: 외래키 없이 `car_number`로 LEFT JOIN합니다(`fetch_reservations`). 그래서 차량번호는 `normalize_car_number()`로 공백을 없앤 형태로 통일해서 저장해야 합니다. 연락처도 `normalize_phone()`으로 `010-1234-5678` 형식으로 저장합니다.
- **업로드 처리 순서**: `api_save_vehicle` → `parse_multipart()` → `save_upload()`.
  - Python 3.13에서 `cgi` 모듈이 제거되었기 때문에 `parse_multipart()`는 경계 문자열을 기준으로 직접 파싱합니다.
  - `save_upload()`는 위험 확장자(`DANGEROUS_EXTENSIONS`, 중간 확장자 포함) → 확장자 허용 목록 → 5MB 제한 → 매직 넘버 검사(`looks_like_image`) → 파일명 정리 순서로 검사합니다.
  - `api_save_vehicle`은 DB 기록이 실패하면 새로 저장한 파일을 지우고, 사진을 교체하면 이전 파일을 지웁니다.
  - 파일명이 겹치면 `_YYYYMMDD_HHMMSS`를 붙이고(`_candidate_names`), `open(..., "xb")`로 저장해서 기존 파일을 절대 덮어쓰지 않습니다.
  - 요청 크기 제한은 `read_body()`가 본문을 읽기 전에 `Content-Length`로 검사합니다.
- **결제**: 시뮬레이션입니다. 카드번호가 `0000`으로 끝나면 402(승인 거절)를 반환합니다. 카드번호 전체는 저장하지 않고 `payment_method`에 끝 4자리만 남깁니다.

프론트엔드(`static/app.js`)는 하나의 페이지 안에서 탭을 전환하는 구조입니다. 해시(`#items`, `#reserve`, `#history`, `#vehicles`)로 탭을 고르고, 탭을 열 때마다 `tabLoaders`가 데이터를 다시 불러옵니다.
DOM은 `h(tag, attrs, ...children)` 도우미로 만듭니다. XSS를 막기 위해 사용자 데이터에는 `innerHTML`을 쓰지 마세요.
업로드 제한, 예약 가능 기간(`MAX_ADVANCE_DAYS`), 결제 기한 등 설정값의 기준은 `server.py` 상수입니다. 화면은 시작할 때(`start()`) `GET /api/config`로 받아 `config` 객체를 덮어씁니다. 화면에서도 쓰는 설정을 새로 만들면 `api_get_config`에 함께 추가하세요.

## 주의 사항

- DB는 WAL 모드라 `data/`에 `-wal`, `-shm` 파일이 함께 생깁니다. DB를 초기화할 때는 세 파일을 모두 지우세요.
- `Handler.timeout`(30초)은 소켓 읽기 한 번 기준입니다. 전체 요청 시간 제한이 아닙니다.

- 정적 파일의 Content-Type은 `CONTENT_TYPES` 딕셔너리로 직접 지정합니다. Windows에서는 `mimetypes`가 레지스트리 설정 때문에 `.js`를 `text/plain`으로 돌려줄 수 있어서 쓰지 않습니다.
- 업로드 파일명에 한글(`\w`)은 허용하고, Windows 예약어(`CON`, `NUL` 등)는 이름 앞에 `file_`을 붙입니다.
