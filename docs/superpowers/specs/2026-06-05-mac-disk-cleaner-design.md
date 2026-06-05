# Mac 용량정리 도구 — 설계 (Spec)

날짜: 2026-06-05
상태: 승인됨 → 구현계획 대기

## 1. 목적

macOS에서 디스크 용량 부족을 해결한다. 무엇이 용량을 차지하는지 스캔해
카테고리별로 분류하고, 웹 GUI에서 체크박스로 삭제할 항목을 선택한 뒤
**휴지통으로 이동**(되돌리기 가능)한다.

성공 기준:
- 의존성 0 (Python3 표준 라이브러리만, pip 설치 없음).
- 삭제는 항상 사용자 확인 후, 영구삭제 아님(휴지통 이동).
- 4개 카테고리 스캔: 시스템 캐시/로그, 개발 캐시, 대용량 파일, 중복/다운로드.
- 카테고리 접기/펼치기 + 카테고리/파일별 체크 선택.

## 2. 비목표 (YAGNI)

- 영구삭제(`rm -rf`) 옵션 — 휴지통 이동만. (추후 필요시 추가)
- 클라우드/외장 디스크 스캔 — 로컬 홈 디렉토리만.
- 주기적 자동실행(cron/launchd) — 이번 범위 아님.
- 로그인/인증 — localhost 전용이라 불필요.

## 3. 아키텍처

단일 Python3 스크립트 `disk_cleaner.py`. 표준 라이브러리만 사용:
`http.server`, `socketserver`, `os`, `pathlib`, `hashlib`, `json`,
`subprocess`, `webbrowser`, `shutil`.

흐름:
```
[스캔]   카테고리별 파일 목록 + 크기 수집 → JSON 인벤토리
   ↓
[웹서버] 127.0.0.1:PORT 바인드 → 브라우저 자동 오픈 → HTML+데이터 전달
   ↓
[삭제]   체크박스 선택 → POST /delete → 휴지통 이동 → 확보용량 보고
```

localhost(`127.0.0.1`) 전용 바인드. 외부 네트워크 접근 차단.

## 4. 컴포넌트

### 4.1 스캐너 (카테고리당 함수 1개)

각 항목 반환 형식:
```python
{
  "path": "/Users/.../Caches/com.apple.Safari",
  "size": 838860800,          # bytes
  "category": "system_cache",
  "label": "com.apple.Safari",
  "default_checked": True
}
```

| 함수 | 대상 경로 | default_checked |
|---|---|---|
| `scan_system_cache` | `~/Library/Caches`, `~/Library/Logs`, `~/.Trash` (하위폴더별 집계) | True |
| `scan_dev_cache` | `~/.npm`, `~/.cache`, `brew --cache`, Xcode `DerivedData`, `~/Library/Developer/CoreSimulator` (존재하는 것만) | True |
| `scan_large_files` | `~` 하위, 임계값↑ (기본 500MB), `os.walk` | False |
| `scan_duplicates` | `~/Downloads`, `~/Desktop`, `~/Documents` — 크기 같은 것끼리 `shasum`(sha256) 비교, 중복쌍의 사본만 후보 | False |

크기 집계: 폴더는 `os.walk` 합산. 권한거부/사라진 파일은 건너뜀.

### 4.2 웹서버 (`CleanerHandler` — `http.server.BaseHTTPRequestHandler`)

| 라우트 | 동작 |
|---|---|
| `GET /` | HTML 페이지 반환 (스캔 데이터 JSON 임베드) |
| `POST /delete` | body `{paths:[...]}` → 휴지통 이동 → `{freed:bytes, failed:[{path,reason}]}` |
| `POST /rescan` | 재스캔 → 새 인벤토리 JSON 반환 |

`127.0.0.1`에만 바인드. 포트는 비어있는 것 자동 탐색(기본 8765부터).

### 4.3 삭제기 (`move_to_trash(path)`)

- macOS `osascript`로 Finder 휴지통 이동 → 메타데이터 보존, 사용자가 되돌리기 가능.
  ```
  osascript -e 'tell app "Finder" to delete POSIX file "<path>"'
  ```
- 호출 전 **보호경로 화이트리스트** 검사. 매칭되면 거부.
- 실패 시 예외 잡아 `failed[]`에 기록, 전체 중단 안 함.

### 4.4 보호경로 (절대 삭제 금지)

`/System`, `/Library`(루트), `/Applications`, `/usr`, `/bin`, `/sbin`,
`~/Library`(직접), `~`(홈 루트 자체), `~/Documents`·`~/Desktop`의 폴더 자체.
삭제 대상 경로가 이 목록에 정확히 일치하거나 상위이면 거부.

## 5. UI 레이아웃

```
┌────────────────────────────────────────────────┐
│  🧹 Mac 용량정리       디스크: 92GB 빈공간 / 228GB │
│  선택됨: 4.2 GB        [ 재스캔 ]                  │
├────────────────────────────────────────────────┤
│ ▼ ☑ 시스템 캐시/로그              2.1 GB          │
│     ☑ com.apple.Safari/...        800 MB         │
│     ☑ Logs/DiagnosticReports      450 MB         │
│ ▼ ☑ 개발 캐시                     1.8 GB          │
│     ☑ ~/.npm                      600 MB         │
│     ☑ Xcode/DerivedData           1.2 GB         │
│ ▶ ☐ 대용량 파일 (기본 해제)        12 GB           │
│ ▶ ☐ 중복/다운로드 (기본 해제)      3 GB            │
├────────────────────────────────────────────────┤
│            [ 선택항목 휴지통 이동 → ]              │
└────────────────────────────────────────────────┘
```

동작:
- 카테고리 헤더 `▼/▶` 접기/펼치기. 헤더 체크박스 = 하위 전체 토글(indeterminate 지원).
- 파일별 개별 체크. 선택 합계 상단 실시간 갱신.
- 하단 버튼 → 확인 모달(`N개 / X GB 휴지통 이동?`) → 실행 → 결과 토스트.
- 순수 HTML + CSS + 바닐라 JS, 전부 인라인. 외부 CDN 0.

## 6. 에러처리

- 권한거부 파일 → 건너뛰고 `failed[]` 표시, 중단 안 함.
- 스캔 중 사라진 파일 → 무시.
- 휴지통 이동 실패 → 사유와 함께 결과에 표시.
- 포트 사용중 → 다음 포트 자동 시도.

## 7. 테스트

- 임시 더미 디렉토리(`tempfile.mkdtemp`) 생성 → 가짜 캐시/대용량/중복 파일 배치.
- 스캔 함수가 올바른 크기/카테고리 반환하는지 검증.
- 보호경로 검사가 시스템 경로를 거부하는지 검증.
- 삭제 함수가 더미 파일만 옮기고 보호경로는 거부하는지 검증.
- 실제 시스템 캐시는 테스트에서 건드리지 않음.

## 8. 파일 구조

```
mac-disk-cleaner/
├── disk_cleaner.py        # 단일 실행 스크립트 (스캔+서버+삭제)
├── test_disk_cleaner.py   # 더미 디렉토리 기반 테스트
├── README.md              # 실행법: python3 disk_cleaner.py
└── docs/superpowers/specs/2026-06-05-mac-disk-cleaner-design.md
```
