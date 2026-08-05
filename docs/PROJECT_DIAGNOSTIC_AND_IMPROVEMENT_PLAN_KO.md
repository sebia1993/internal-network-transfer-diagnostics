# 사내 파일 전송 및 네트워크 체크 프로젝트 진단 및 개선 계획

작성 기준: 2026-08-05 현재 작업트리
대상 버전: 정식 릴리스 준비 중인 소스 `v0.5.1`
대상 사용자: 초급 네트워크 엔지니어, 상급 관리자 및 IT 관리 책임자

## 보고서의 판단 기준

이 문서는 README의 설명보다 실제 소스와 테스트를 우선한다. 판단 근거는 다음 네 가지로 구분했다.

- **코드에서 확인된 사실**: 현재 파일, 함수, 상수, 테스트에서 직접 확인했다.
- **실행 또는 테스트가 필요한 사항**: 자동 테스트만으로 사용자 환경의 동작을 증명할 수 없다.
- **합리적으로 예상되는 위험**: 현재 구조상 발생 가능성이 있지만 이번 점검에서 재현하지는 않았다.
- **추가 확인이 필요한 사항**: 운영망, 실제 장비, 배포 EXE 또는 장시간 시험이 필요하다.

이번 작업트리에서 `pytest` 전체 458건, 장애 주입 32건, Python `compileall`, 5개 JavaScript 파일의 `node --check`, `git diff --check`를 통과했다. 실제 Windows 자원 계측 soak는 2,708.89초 동안 386 cycles, 업로드 101,187,584 bytes, TCP 자체 점검 386회를 완료했다. initial·restart 합계 772개 프로세스에서 1,544개 표본을 수집했고 working set, handle, thread, TCP socket 네 지표가 모두 `available`이었다. 분석기의 데이터 품질은 `pass`, issues 0건, review findings 0건이며 최종 판정은 `PASS_NO_REPEATED_PROCESS_GROWTH`였다. initial working set 기준/최종값은 54,228,992/55,867,392 bytes(+1,638,400), restart는 56,008,704/57,176,064 bytes(+1,167,360)로 16,777,216-byte review 기준보다 낮았고 handle·thread·socket 최종값도 안정적이었다. 다만 각 cycle의 initial과 restart는 서로 다른 프로세스이므로 이 결과는 반복 재기동 시 자원 증가 추세가 없었다는 근거이지 단일 프로세스의 장기 누수 부재를 증명하지 않는다. Chrome과 Edge 화면 검증은 Computer Use의 URL 안전 확인이 3회 연속 실패해 캡처와 클릭 전에 중단됐다. 이 중단은 앱 오류로 확인된 사항이 아니며, 화면 배치와 실제 보조기술 동작은 별도 현장 검증 항목으로 남긴다.

---

## 1. 프로젝트 목적과 현재 구조 요약

### 1.1 주요 목적

**코드에서 확인된 사실**

이 프로젝트는 사내 장애 대응 때 파일을 올리고 다운로드 링크를 공유하는 Flask 기반 Windows 도구다. 같은 웹 화면에서 브라우저 HTTP 처리량과 별도 Windows TCP 클라이언트를 이용한 전송 성능도 측정한다.

핵심 기능은 다음과 같다.

1. 파일 업로드, 다운로드 링크 생성, 허용된 IP에서의 삭제
2. HTTP 데이터량 기준 업로드 및 다운로드 측정
3. HTTP 측정 시간 기준 업로드 및 다운로드 측정
4. Windows TCP 클라이언트를 이용한 업로드 및 다운로드 측정
5. CSV, JSON, Excel 결과 저장과 조회
6. 상태 API와 관리자용 운영 요약

주요 진입점은 `app.py`의 `create_app()`과 `main()`이다.

### 1.2 사용자 입력부터 결과까지의 흐름

#### 파일 업로드

1. 사용자가 첫 화면의 `파일 업로드` 탭에서 파일, 하위 폴더, 메모를 입력한다. 화면은 `templates/index.html`의 `upload-mode`에 있다.
2. `static/network_check.js:65` 이후의 제출 방지 로직이 중복 클릭을 막고 상태를 표시한다.
3. `app.py`의 `upload()`가 `POST /upload` 요청을 받는다.
4. 파일명, 확장자, PE 헤더, 저장 경로, 동시 업로드 수, 디스크 예약 용량을 검사한다. 주요 함수는 `app.py`의 `safe_filename()`, `blocked_upload_reason()`, `reserve_upload_target()`이다.
5. 같은 폴더의 임시 파일에 기록하고 `fsync`한 뒤 원자적으로 최종 파일로 교체한다. 파일 확정과 CSV 기록 사이에는 `upload_transactions.py`의 트랜잭션 표식을 사용한다.
6. `data/upload_log.csv`에 기록한 뒤 다운로드 URL을 반환한다.
7. 다음 시작 때 `app.py`의 `recover_upload_transactions()`가 미완료 업로드 또는 삭제를 복구한다.

#### HTTP 데이터량 기준 측정

1. 사용자가 데이터량과 방향을 고른다.
2. 다운로드는 `GET /network-check/download`, 업로드는 `/network-check/upload/start`, `/chunk/<session_id>`, `/finish/<session_id>` API를 순서대로 사용한다. 라우트는 `app.py`에 있다.
3. 브라우저는 진행률, 평균 속도, 최근 속도, 다음 조치를 갱신한다. 관련 로직은 `static/network_check.js:157` 이후에 있다.
4. 서버는 유효한 전송량과 시간을 확인하고 `data/network_check_log.csv`에 기록한다.
5. 전체 측정 중 한 방향이 실패하면 완료된 방향의 결과를 지우지 않고 부분 완료로 표시한다. 이 동작은 `tests/test_frontend_ux.py`에서 검사한다.

#### HTTP 측정 시간 기준

1. `network_sustained.py`의 `POST /network-check/sustained/sessions` 라우트가 세션을 만든다.
2. 각 방향에서 3초 워밍업과 10초 또는 30초 측정을 진행한다.
3. 브라우저와 서버가 계산한 전송량, 구간 합계, 실제 시간을 비교한다.
4. `network_sustained.py`의 `SustainedCheckManager.complete()`가 결과를 JSON과 CSV에 저장한다.
5. 사용자는 화면 그래프와 Excel을 내려받을 수 있다.
6. 전체 측정에서 한 방향만 유효하게 끝난 경우 완료된 방향은 `부분 완료`로 보존한다. `static/network_sustained.js`와 `tests/test_frontend_ux.py`가 이 분기를 고정한다.

#### TCP 전송 성능 측정

1. Windows 클라이언트가 등록 API를 호출하고 서버가 연결 가능 상태를 확인한다. 라우트는 `network_probe/routes.py:128-267`에 있다.
2. 웹 화면에서 준비 완료 클라이언트, 방향, 시간, 스트림 수를 고른다.
3. `ProbeService`가 작업을 배정하고 `network_probe/tcp_engine.py`가 별도 TCP 데이터 연결을 처리한다.
4. 결과 제출 시 본문 크기, 필드, 배열 개수, 수치 범위, 스트림 및 구간 합계를 검증한다.
5. `network_probe/service.py`의 `ProbeService._finalize_session()`가 소켓을 정리하고 JSON·CSV 저장을 시도한 뒤 측정 잠금을 해제한다.
6. 웹 화면은 요약, 그래프, 기술 상세, Excel 링크를 표시한다.

### 1.3 주요 화면과 메뉴

**코드에서 확인된 사실**

화면은 한 페이지이며 다음 순서로 구성된다.

1. 관리자용 운영 요약: `templates/index.html`의 상단 `data-operations-dashboard`
2. 상위 탭 `파일 업로드`, `네트워크 체크`: `upload-mode`, `network-mode`
3. 네트워크 체크 안의 `HTTP 전송 측정`, `TCP 전송 성능 측정`: `data-measurement-mode`
4. HTTP 안의 `데이터량`, `측정 시간` 선택: `data-http-criterion`
5. 각 방식의 상태, 진행률, 결과, 그래프, 기술 상세: `data-network-check` 아래 결과 패널

탭은 키보드 방향키, Home, End를 지원한다. 상태 영역은 `role=status`, 오류는 `role=alert`, 진행률은 `role=progressbar`와 `aria-valuenow`, `aria-valuetext`를 사용한다.

버튼과 입력란의 기본 목적은 화면 문구로 확인된다. 파일 선택, 저장 하위 폴더, 메모, 업로드 버튼은 한 form에 있고, 각 측정 카드에는 데이터량 또는 시간·방향·시작·취소가 순서대로 있다. 다만 `전체 측정`, `데이터량`, `측정 시간`, `TCP 전송 성능`의 차이는 설명 문장을 읽어야 이해할 수 있어 초급 사용자의 무설명 과업 시험이 필요하다.

### 1.4 실행 환경과 설치 방식

**코드에서 확인된 사실**

- 일반 사용자는 Windows용 PyInstaller onedir ZIP을 내려받아 `start_internal_upload.cmd`를 실행한다.
- 소스 실행은 `run.bat`을 사용한다.
- 소스 의존성은 Flask 3.1.3, openpyxl 3.1.5, pytest 8.4.2다.
- Windows 릴리스 잠금 파일은 CPython 3.11 x64와 PyInstaller 6.21.0을 기준으로 해시를 고정한다.
- 웹 기본 포트는 8000, TCP 기본 포트는 5201이다.
- 관리자 권한을 요청하거나 방화벽을 자동 변경하지 않는다.

**추가 확인이 필요한 사항**

현재 소스 버전은 `v0.5.1`로 올렸지만, Windows ZIP은 clean commit 빌드, 서버·클라이언트 자체 점검, ZIP verifier와 SHA256 대조를 모두 통과한 뒤에만 배포 가능으로 판단한다. 기존 `v0.4.6` Release asset에는 이번 변경이 포함되지 않고, `v0.5.0` 태그는 게시 전 workflow에서 중단돼 Release asset이 없다.

별도 설명 없이 설치·실행할 수 있는 수준은 아니다. 포터블 ZIP 안의 시작 CMD로 실행 자체는 단순하지만, 사용자는 실행용 asset과 GitHub 소스 ZIP을 구분하고 압축을 완전히 풀어야 하며 SmartScreen, 방화벽과 TCP 클라이언트 준비 절차는 README가 필요하다. 필수 Python·라이브러리는 소스 실행 사용자에게만 필요하고, 정식 onedir ZIP 사용자는 내장 런타임을 사용한다.

### 1.5 설정 파일과 사용자가 바꿀 수 있는 값

`config.ini`의 공개 설정은 다음과 같다.

| 섹션 | 항목 | 용도 | 검증 |
|---|---|---|---|
| app | `CONFIG_VERSION` | 설정 마이그레이션 버전 | 0부터 현재 버전 2 |
| app | `HOST` | 웹 바인딩 주소 | IPv4 또는 포트 없는 호스트 이름 |
| app | `PORT` | 웹 포트 | 1부터 65535 |
| app | `BASE_URL` | 다운로드 링크 기준 주소 | 빈 값 또는 userinfo/query/fragment 없는 HTTP(S) URL |
| app | `STORAGE_ROOT` | 업로드 저장 기준 폴더 | 비어 있지 않은 폴더 경로, 상대경로면 설정 파일 기준 |
| app | `DELETE_ALLOWED_IPS` | 삭제 허용 IP 목록 | CIDR이 아닌 개별 IPv4/IPv6 |
| app | `RECENT_LIMIT` | 최근 업로드 표시 개수 | 1부터 10000 |
| network_probe | `ENABLED` | TCP 측정 서버 사용 여부 | ConfigParser 불리언 별칭 |
| network_probe | `PORT` | TCP 데이터 포트 | 1부터 65535 |

`startup_ports.py`의 `_validate_config_values()`와 문자열별 validator가 숫자, 불리언, URL, 경로, 호스트와 IP 목록을 검사한다. 잘못된 값은 기본값으로 숨기거나 원문을 출력하지 않고 시작을 중단한다. `load_config()`는 저장 경로가 기존 일반 파일이거나 경로 해석에 실패하는 경우도 `ConfigFileError`로 변환한다.

### 1.6 외부 연동 구조

**코드에서 확인된 사실**

- 데이터베이스와 외부 SaaS API는 사용하지 않는다.
- 운영 데이터는 로컬 파일에 저장한다.
- 브라우저는 Flask HTTP API만 호출한다.
- TCP 측정은 별도 Windows 클라이언트와 자체 프로토콜 v2로 통신한다.
- 서버는 방화벽, 레지스트리, 시작 프로그램, 예약 작업을 변경하지 않는다.
- 측정과 자체 점검은 조회 및 임시 전송 데이터만 사용하며 네트워크 장비 설정을 변경하지 않는다.

### 1.7 주요 모듈의 역할

| 파일 | 역할 |
|---|---|
| `app.py` | Flask 앱, 업로드, 다운로드, 삭제, 빠른 HTTP 측정, 상태 API, 시작 및 종료 |
| `bounded_server.py` | 최대 32개 요청 스레드, 30초 무활동 제한, 503 거절, 종료 drain |
| `startup_ports.py` | 설정 파싱과 검증, 웹/TCP 포트 충돌 처리, 설정 원자적 저장 |
| `runtime_stability.py` | CSV 무결성, 백업과 보관, 디스크 검사, 업로드 예약, 데이터 폴더 잠금, 로그 순환 |
| `upload_transactions.py` | 업로드와 삭제의 crash-recovery 트랜잭션 표식 |
| `measurement_transactions.py` | HTTP 시간 기준과 TCP 상세 JSON·요약 CSV의 durable intent, rollback 상태와 시작 재조정 |
| `network_measurement.py` | 서버 전체에서 네트워크 측정 한 건만 허용하는 전역 gate |
| `network_sustained.py` | HTTP 측정 시간 기준 세션, 검증, 저장, 종료 |
| `network_probe/service.py` | TCP 에이전트, 세션, 작업, 결과 저장과 정리 |
| `network_probe/tcp_engine.py` | TCP 데이터 스트림 송수신과 타임아웃 |
| `network_probe/protocol.py` | 길이 제한 프레임 프로토콜 |
| `result_storage.py` | JSON 원자적 저장과 오래된 결과 정리 |
| `excel_report.py`, `sustained_excel.py`, `network_probe/excel.py` | Excel 결과 생성 |
| `templates/index.html`, `static/*.js`, `static/style.css` | 단일 페이지 UI, 상태, 진행률, 그래프, 접근성 |

#### 핵심 클래스와 함수의 책임 및 호출 관계

| 클래스/함수 | 실제 책임 | 주요 호출 관계 |
|---|---|---|
| `NetworkMeasurementGate` (`network_measurement.py:27`) | 빠른 HTTP, 시간 기준 HTTP, TCP 중 하나만 측정하도록 owner와 session ID를 원자적으로 관리 | 각 측정 시작 시 `acquire()`, 완료·실패·취소 시 `release()`, 절대 상한 시 등록된 취소 callback 호출 |
| `SustainedCheckManager` (`network_sustained.py`) | HTTP 시간 기준 세션 순서, 바이트·시간 검증, 만료, 결과 JSON·CSV 저장 | `create_sustained_blueprint()`가 API에 연결하고 `create_app()`이 공통 gate와 진단 logger를 주입 |
| `ProbeService` (`network_probe/service.py`) | TCP listener, agent 등록, 측정 job, 스트림·결과 검증, terminal session 정리와 결과 저장 | `create_app()` 및 probe blueprint가 상태/API를 노출하고 `main()`이 listener의 시작·종료를 담당 |
| `BoundedThreadedWSGIServer` (`bounded_server.py:27`) | 요청 슬롯 32개, 소켓 무활동 제한, 과부하 503, active request drain | `main()`이 `make_bounded_server()`로 생성하고 종료 시 `begin_shutdown()`·`wait_for_active_requests()`·`force_close_active_requests()` 순서로 호출 |
| `begin_upload_transaction()`·`advance_upload_transaction()`·`finish_upload_transaction()` | 파일/CSV 변경 전 durable marker를 만들고 단계별 commit 상태를 원자적으로 기록·정리 | `app.py`의 업로드·삭제 경로와 `recover_upload_transactions()`가 재시작 복구에 사용 |
| `commit_measurement_result()`·`recover_measurement_transactions()` | durable marker, JSON 원자 확정, CSV 행별 `fsync`, rollback 의도와 시작 재조정 | HTTP 시간 기준과 TCP 저장기가 공통 사용하며 source별 결과→CSV 행 builder를 복구 때 다시 실행해 marker와 대조한다 |
| `build_measurement_activity()`·`build_recent_measurement_changes()` | 원시 측정 행을 완료/취소/실패 표본과 같은 방식·방향의 상태 전환으로 변환 | `/api/operations-summary`가 읽기 전용으로 호출하고 `operations_dashboard.js`가 관리자 카드에 표시 |

### 1.8 로그와 오류 처리

운영 진단 로그는 `runtime_stability.py`의 `configure_diagnostic_logger()`가 `data/diagnostics/internal-upload.log`에 기록한다. 파일당 2MiB, 백업 5개로 순환한다. 측정 취소 콜백, HTTP/TCP 결과 저장, CSV 보관과 만료 정리 실패는 안정된 이벤트명과 예외 종류만 남기고 예외 메시지는 기록하지 않는다. 누적 횟수는 `/api/health`의 `checks.background_tasks`에도 표시한다.

사용자 오류는 HTTP 상태, 한국어 안내와 `UPLOAD_PROCESSING_FAILED`, `DELETE_PROCESSING_FAILED`, `RESULT_WRITE_FAILED`, `MEASUREMENT_RECOVERY_PENDING`, `STORAGE_INIT_FAILED`, `TCP_BIND_FAILED`, `WEB_BIND_FAILED` 같은 안정된 코드로 반환하고 개발자 진단은 파일 로그로 분리한다. 설정 파일 형식, 인코딩, 의미 오류와 손상된 업로드 복구 표식은 traceback 없이 시작 실패로 안내하고 설정의 절대 경로 대신 파일명만 표시한다. 사용자가 제안된 웹 포트 변경을 거절하면 `WEB_PORT_CHANGE_DECLINED`와 종료 코드 2로 서버를 시작하지 않는다. HTTP 시간 기준과 TCP 측정은 같은 source의 미정리 marker가 있으면 세션과 전송을 만들기 전에 503으로 거절한다. TCP는 gate 획득 직후 marker가 생기는 경쟁도 재검사하고 gate를 해제하며, 저장 직전 발견된 복구 오류를 일반 `RESULT_WRITE_FAILED`로 덮어쓰지 않는다. 저장, 포트, TCP 서비스와 클라이언트 ZIP 오류는 실패 사실을 정상으로 바꾸지 않는다.

HTTP 시간 기준과 TCP blueprint의 응답에는 `Cache-Control: no-store`와 `X-Content-Type-Options: nosniff`가 공통 적용된다. 파일 다운로드와 측정 결과 다운로드에도 같은 목적의 캐시 금지 및 MIME 추측 방지 헤더를 사용한다.

### 1.9 테스트 구조와 범위

현재 25개 `test_*.py` 파일의 전체 회귀는 pending marker 시작 차단, 결과 JSON 단일 읽기와 안전한 읽기 실패, 응답 헤더, repository runtime template와 soak 분석기, 릴리스 빌드 실패 전파 계약을 포함해 458개 테스트를 수집하고 모두 통과했다. 장애 주입 suite도 32개 모두 통과했다.

- Flask API, 업로드, 다운로드, 삭제, 상태: `tests/test_app.py`
- 요청 제한과 종료: `tests/test_bounded_server.py`
- 실제 하위 프로세스 강제 종료: `tests/test_fault_injection.py`
- 프런트엔드 구조와 접근성: `tests/test_frontend_ux.py`
- 전역 측정 gate: `tests/test_network_measurement.py`
- TCP 서비스, API, 프로토콜, 제한: `tests/test_network_probe*.py`
- HTTP 측정 시간 기준: `tests/test_network_sustained.py`
- 설정과 포트, main 종료: `tests/test_startup_ports.py`
- CSV, 잠금, 디스크, 로그: `tests/test_runtime_stability.py`
- 업로드 트랜잭션과 복구: `tests/test_upload_transaction*.py`
- 측정 트랜잭션의 semantic 대조, rollback 부분 실패와 시작 복구: `tests/test_measurement_transactions.py`
- Windows 반복 시험 도구와 분석기: `tests/test_windows_stability_soak.py`, `tests/test_windows_soak_analysis.py`

### 1.10 종료 시 자원 정리

`app.py`의 `shutdown_network_measurements()`가 빠른 HTTP 세션, HTTP 시간 기준 세션, TCP 세션을 정리한다. `main()`은 다음 순서를 사용한다.

1. 새 웹 요청을 거절한다.
2. 진행 중 요청을 최대 30초 기다린다.
3. 남은 소켓에 종료를 요청하고 측정 관리자에 정리를 요청한다.
4. 2초를 더 기다린다.
5. 요청 스레드가 계속 남으면 데이터 잠금을 풀지 않은 상태에서 진단 로그를 flush하고 종료 코드 2로 프로세스를 끝낸다.
6. 정상 경로에서는 TCP 서버, 웹 서버, 로그 핸들러, 데이터 잠금을 닫는다.

Windows의 buffered read는 소켓 `SHUT_RDWR`만으로 항상 풀리지 않는다. 이 경우 hard exit가 마지막 안전장치다.

### 1.11 프로젝트 설명과 실제 코드의 차이

**코드에서 확인된 사실**

- 현재 README와 repo-local `AGENTS.md`는 파일 업로드뿐 아니라 빠른 HTTP, 시간 기준 HTTP, TCP 측정과 읽기 전용 운영 요약까지 설명하도록 이번 작업트리에서 갱신했다.
- GitHub의 기존 정식 `v0.4.6` ZIP은 이번 `v0.5.1` 소스보다 이전 상태다. `v0.5.0`은 tag workflow가 빌드 전에 중단돼 실행 asset이 없다. 따라서 이 문서의 기능 “완료” 표시는 소스와 자동 회귀 범위에 적용하며, 배포 완료 표시는 별도의 clean ZIP·태그 workflow·게시 asset 검증을 통과해야 한다.
- 운영 요약은 장비 인벤토리나 incident 관리 기능이 아니라 최근 측정 표본과 서버 기능 상태의 요약이다. 화면과 README에 이 범위를 명시했다.

---

## 2. 전체 평가 점수

점수는 현재 작업트리를 기준으로 매겼다. 실제 운영 장비와 배포 EXE 검증이 끝나지 않았으므로 자동 테스트만으로 10점을 부여하지 않았다.

| 평가 항목 | 점수 | 판단 |
|---|---:|---|
| 초급 엔지니어 사용성 | 7.5/10 | 설치 문서, 작업 탭, 진행 상태, 안전한 오류 코드와 다음 조치가 있다. 일반 파일 업로드의 바이트 진행률, 실제 브라우저 접근성, 수동 방화벽과 SmartScreen 대응은 남아 있다. |
| 관리자 사용성 | 6.5/10 | 첫 화면에 완료·취소·실패 표본, 문제 우선 정렬, 기능별 상태, 실제 상태 전환과 권장 조치가 있다. 장비 수, 장애 지속 시간, 영향 대상, 미조치·조치 이력과 공유 보고서는 없다. |
| 프로그램 안정성 | 9.0/10 | 업로드·삭제와 측정 JSON-CSV crash recovery, pending marker의 시작 전 503 차단, 타임아웃, 동시성 제한과 실제 2,708.89초 Windows soak의 반복 프로세스 자원 추이 PASS 근거가 있다. 단일 프로세스 장기 누수와 새 EXE 검증은 남아 있다. |
| 유지보수성 | 6.5/10 | 네트워크, 저장, Excel, 시작 코드가 모듈로 나뉘고 테스트 경계가 좋다. `app.py` 3,059줄, `ProbeService` 1,581줄과 일부 중복 저장 정책은 변경 영향 범위를 키운다. |

---

## 3. 가장 잘 구현된 부분

### 3.1 파일과 CSV의 일관성

업로드와 삭제는 `upload_transactions.py`의 prepared, committed, rolled_back 단계를 사용한다. 정상 완료 표식은 다음 시작 때 cleanup-only로 처리하고, 미완료 경로가 다른 업로드 기록에 재사용되면 자동 추정하지 않고 시작을 중단한다. 프로세스를 실제로 강제 종료하는 테스트가 파일 확정 직후와 삭제 CSV 갱신 직후를 검증한다.

### 3.2 잘못된 성공 판정 방지

HTTP와 TCP 측정은 전송량 0, 누락된 시간, 빈 결과, 구간/스트림 합계 불일치, 클라이언트와 서버의 큰 전송량 차이를 성공으로 저장하지 않는다. 일부 방향만 완료된 전체 측정은 성공으로 뭉개지 않고 부분 완료를 보존한다.

### 3.3 동시성 및 종료 경계

웹 요청 32개, 파일 업로드 4개, 전역 네트워크 측정 1개, TCP 에이전트 256개로 상한이 있다. 취소 콜백은 세션 ID를 캡처해 늦게 실행된 콜백이 새 세션을 종료하지 않는다. 종료 중 요청 스레드가 남으면 데이터 잠금을 먼저 풀지 않는다.

### 3.4 사용자 메시지와 운영 진단 분리

초급 사용자는 한국어 상태와 다음 조치를 보고, 개발자는 순환 진단 로그에서 오류 종류와 단계 정보를 확인할 수 있다. IP, PC 이름, 세션 ID, 인증 토큰, 원시 오류는 관리자 요약 API에서 제외한다.

### 3.5 테스트 가능한 경계

Flask test client, `FakeClock`, 임시 폴더, 로컬 루프백 TCP, 실제 하위 프로세스 강제 종료를 조합한다. 실제 운영 장비나 고객 자료 없이 타임아웃, 저장 실패, 충돌, 재시작을 재현할 수 있다.

### 3.6 실제 Windows 반복 안정성 계측

`tools/run_windows_stability_soak.py`는 2,708.89초 동안 386회의 256KiB 업로드, 서버 강제 종료·재시작, 기존 파일 다운로드, TCP 자체 점검을 완료했다. 772개 프로세스에서 1,544개 표본을 모았고 네 자원 지표가 모두 `available`이었다. `tools/analyze_windows_soak_summary.py`는 실행 시간, 기능 횟수, 프로세스·표본 구조와 마지막 구간 coverage를 먼저 검사한 뒤 네 종류의 자원 anomaly를 평가했다. 데이터 품질 `pass`, issues 0건, findings 0건, `PASS_NO_REPEATED_PROCESS_GROWTH`를 반환했다.

### 3.7 저장소 예시 데이터와 민감 응답 방어

`tests/test_app.py::test_repository_runtime_templates_are_sanitized_and_header_only`는 추적된 `config.ini`가 기본 예시값이고 네 운영 CSV가 정확히 헤더 1행뿐인지 검사한다. HTTP 시간 기준·TCP API와 다운로드 응답에는 `no-store` 및 `nosniff`가 적용된다. 이는 브라우저 캐시와 우발적 저장소 오염 위험을 줄이지만, 실행 중 추적 파일이 실제 운영값으로 바뀐 뒤 사용자가 `git add -A`를 수행하는 행위까지 막지는 못한다.

---

## 4. 가장 위험한 문제

### 4.1 인증과 TLS가 없는 사내 웹 서비스

**코드에서 확인된 사실**

업로드와 측정 시작에는 사용자 인증이 없다. 삭제만 접속 IP 허용 목록을 검사한다. HTTP 평문 사용이 기본이며 TCP agent의 Bearer token과 session token도 TLS 없이 HTTP 제어 채널로 전달된다. 토큰은 메모리에서 난수로 생성하고 로그와 클라이언트 ZIP에는 넣지 않지만, 신뢰되지 않는 구간의 도청 방어는 제공하지 않는다.

**영향**

신뢰하지 않는 단말이 같은 네트워크에 있으면 파일 업로드로 디스크와 서버 부하를 유발할 수 있다. 다운로드 URL을 알면 파일을 받을 수 있다.

**권고**

현재 릴리스의 흐름을 조용히 바꾸지 말고, 신뢰된 관리망에서만 운영한다는 조건을 유지한다. 인증이나 TLS가 필요하면 기존 URL과 TCP 클라이언트 호환성을 포함한 별도 버전 계획으로 다룬다.

### 4.2 측정 JSON과 CSV 사이의 프로세스 종료 경계 — 이번 작업트리에서 해소

**코드에서 확인된 사실**

`measurement_transactions.py`가 HTTP 시간 기준과 TCP 결과의 예상 CSV 행, session ID와 상세 JSON semantic hash를 durable marker에 먼저 기록한다. 이후 JSON을 원자 확정하고 CSV 각 행을 `flush`·`fsync`한 뒤 marker를 제거한다. 일반 저장 예외가 발생하면 marker를 먼저 `rollback_requested`로 durable 전진시킨 뒤 JSON과 해당 session CSV 행을 idempotent하게 정리한다. `ensure_directories()`는 CSV 꼬리 무결성 검사 뒤, JSON pruning과 CSV archive보다 먼저 남은 marker를 재조정한다.

**실행으로 확인된 사실**

HTTP와 TCP 각각에서 JSON 확정 직후, `full` 첫 CSV 행 확정 직후 하위 프로세스를 실제로 kill했다. 재시작은 누락 행만 추가했고, 두 번째 재조정에서는 CSV·JSON bytes가 바뀌지 않았다. 복구 때 source별 `build_sustained_log_rows()` 또는 `build_probe_log_rows()`로 상세 JSON에서 예상 행을 다시 만들고 marker의 모든 필드와 비교한다. 해시 불일치, marker/JSON 의미 불일치, 같은 key의 다른 행, 중복 행, JSON 없는 CSV 행, 예상하지 못한 transaction 항목과 손상 marker는 자동 추정하지 않고 fail-closed 한다. CSV 정리만 실패하거나 JSON 삭제만 실패하는 rollback 두 조합도 다음 시작에서 완전 정리되고 두 번째 재시작이 bytes를 바꾸지 않는지 검증했다.

**권고**

현재 marker→JSON→행별 CSV→marker 정리 순서와 4개 process-kill 회귀를 유지한다. marker 정리만 실패하면 주 결과는 성공으로 유지하되 diagnostic counter를 올리고 그 회차의 archive·prune을 건너뛴다. 같은 측정 source의 다음 작업은 측정 시작 전에 `MEASUREMENT_RECOVERY_PENDING` 503으로 막아 세션, watchdog, 네트워크 전송을 만들지 않는다. TCP는 시작 전 검사와 gate 획득 직후 재검사를 함께 사용하고 두 번째 검사에서 marker가 발견되면 gate를 해제한다. 저장 직전 경쟁으로 marker가 발견된 경우도 이 오류를 일반 `RESULT_WRITE_FAILED`로 바꾸지 않는다. 재시작 복구가 끝난 뒤 다시 측정하며, marker 없는 과거 고아 JSON은 근거 없이 자동 복구하거나 삭제하지 않는다.

### 4.3 비정상 종료 뒤 저장소를 외부에서 직접 바꾸는 경우

**코드에서 확인된 현재 방어**

미완료 업로드·삭제 트랜잭션(`upload_transactions.py`)이 가리키는 업로드 파일을 외부 프로그램이 CSV 기록 없이 다른 내용으로 교체하면, 업로드 marker에는 파일 hash나 생성 식별자가 없어 소유권을 구분할 수 없다. JSON semantic hash를 가진 `measurement_transactions.py`의 측정 marker와는 별개의 제한이다.

**현재 방어**

같은 경로를 다른 CSV 행이 사용하면 fail-closed 한다. README는 복구가 끝나기 전 저장소와 CSV, 트랜잭션 폴더를 수동으로 바꾸지 말라고 안내한다.

**중장기 권고**

파일 크기와 hash를 표식에 넣거나, 저장소 독점 소유 정책을 더 강하게 검사한다.

### 4.4 Windows 요청 스레드의 hard-exit 의존

**코드에서 확인된 사실**

일부 buffered read는 소켓 종료 요청만으로 깨지지 않을 수 있다. 30초 drain과 2초 grace 뒤에도 요청이 남으면 `os._exit(2)`를 사용한다.

**영향**

정상적인 Python 정리 코드가 끝까지 실행되지 않는다. 운영체제가 핸들과 잠금을 회수하고 다음 시작이 복구를 맡는다.

**현재 판단**

데이터 잠금을 먼저 해제해 두 서버가 동시에 쓰는 것보다 안전하다. 비정상 종료 경로로 명확히 기록하고 반복 시험을 유지해야 한다.

### 4.5 현재 소스와 배포 ZIP의 차이

**코드에서 확인된 사실**

소스 버전 문자열과 변경 기록은 `v0.5.1`로 정리했다. 기존 `v0.4.6` ZIP에는 이 변경이 없고, 실패 이력인 `v0.5.0` tag에는 Release asset이 없다. `v0.5.1`은 clean source commit과 동일한 원격 annotated tag에서 빌드해야 한다.

**실행에서 확인된 사실**

첫 clean commit 빌드를 Windows PowerShell 5.1에서 실행했을 때 EXE 세 자체 점검은 통과했지만, BOM 없는 UTF-8 `.ps1`의 한국어가 잘못 해석돼 ZIP verifier가 시작 CMD 안내를 거부했다. 이때 Python verifier의 종료 코드 1도 빌드 스크립트가 즉시 전파하지 않아 SHA 파일까지 생성되는 결함을 확인했다. 다음 빌드에서는 `.ps1` BOM으로 한국어는 복구됐지만, PowerShell 5.1의 `Set-Content -Encoding UTF8`이 실행 CMD 앞에 BOM을 붙여 `@echo off` 실행이 실패하는 경계도 확인했다. 실패한 ZIP들은 게시하지 않았다. `build_windows_release.ps1`은 UTF-8 BOM으로 저장하고 CMD는 UTF-8 no-BOM으로 직접 쓰며 version metadata·PyInstaller·보안 산출물·ZIP verifier의 native 종료 코드를 모두 검사한다. Actions의 compile·JS·pytest·fault·dependency 검사도 각 종료 코드를 즉시 전파한다. 회귀와 ZIP verifier는 이 두 인코딩 계약과 실패 전파 지점을 고정한다.

`v0.5.0` annotated tag를 push한 뒤에는 Actions checkout이 해당 로컬 tag ref를 commit object로 평탄화해 annotated type 검사에서 빌드 전에 중단됐다. 원격 tag 자체는 annotated였고 올바른 source commit을 가리켰으며 Release와 asset은 생성되지 않았다. 태그는 이동·삭제하지 않고 실패 이력으로 보존했다. `v0.5.1` workflow는 원격의 정확한 tag ref를 force-fetch한 뒤 object type과 checkout commit을 검사한다.

**영향**

기존 `v0.4.6` GitHub Release ZIP을 설치하면 이 보고서에서 완료로 표시한 개선이 포함되지 않는다. `v0.5.0`에는 실행 asset이 없고, `v0.5.1`도 태그 workflow와 asset 검증이 끝나기 전에는 실행용 소스 ZIP과 혼동하면 안 된다.

**권고**

수정된 `v0.5.1` clean worktree에서 PyInstaller onedir 빌드, ZIP 검증, SHA256/SBOM과 실제 EXE 자체 점검을 다시 수행한다. 같은 source commit을 가리키는 원격 annotated tag가 없으면 release workflow를 중단하고 기존 asset은 덮어쓰지 않는다.

### 4.6 실제 브라우저와 운영망 검증 미완료

**실행 또는 테스트가 필요한 사항**

소스 기반 UI 구조와 로컬 API는 검증했지만, Computer Use의 URL 안전 확인이 3회 연속 실패해 실제 화면 캡처와 클릭 검증을 완료하지 못했다. 이는 애플리케이션 오류가 아니라 자동화 도구의 안전 확인 단계에서 중단된 결과다. 실제 Windows 11 Edge/Chrome, Android Chrome, 스크린리더, 사내 방화벽, 1Gbps 이하 실제 망에서 확인해야 한다.

### 4.7 남은 보안 및 운영 경계

**코드에서 확인된 사실**

- `config.ini`와 네 운영 CSV는 Git에서 추적하는 runtime template다. 자동 테스트가 기본값과 header-only를 검사하지만, 운영 후 수정된 파일을 `git add -A`로 직접 stage하는 행위 자체는 차단하지 않는다.
- TCP 클라이언트 ZIP의 `server_url`은 유효성 검사를 거친 요청 `Host`를 우선 사용한다. 악성 문자는 거절하지만, 신뢰되지 않는 reverse proxy나 임의의 유효 호스트 이름이 들어오면 잘못된 접속 주소를 포함한 ZIP을 만들 수 있다.
- 관리자 요약과 진단 로그는 원시 오류를 숨기지만 상세 JSON과 운영 CSV에는 측정 클라이언트가 보낸 제한 길이 오류가 남을 수 있다. 생성되는 Excel은 수식 시작 문자를 방어하지만 원본 CSV 셀은 직접 Excel로 열 때의 formula 해석을 별도로 중화하지 않는다.
- `/api/health`는 body의 `status=degraded`와 세부 check로 저하를 알리면서 HTTP 상태는 200을 유지한다. 기존 UI와 기존 instance 감지는 이를 읽지만, 상태 코드만 보는 외부 감시는 저하를 놓칠 수 있다.

**권고**

운영 자료가 생긴 작업 폴더에서는 `git add -A` 전에 네 template 파일을 확인하고, 신뢰된 LAN·proxy에서만 실행한다. TCP 클라이언트 URL을 고정해야 하는 배포는 승인된 `BASE_URL` 또는 trusted-host 정책을 별도 호환성 설계로 검토한다. 원본 CSV와 상세 오류는 민감 운영 자료로 취급하고 직접 스프레드시트로 열기 전에 안전한 Excel 내보내기를 우선 사용한다. health 상태 코드를 바꾸면 기존 instance 감지 계약이 달라지므로, 먼저 별도 strict readiness endpoint 또는 모니터의 JSON body 판독을 검토한다.

### 4.8 단일 프로세스 장기 누수는 아직 미증명

실제 45분 soak의 `PASS_NO_REPEATED_PROCESS_GROWTH`는 386회의 initial/restart 프로세스 사이에서 반복적으로 높아지는 자원 패턴이 없었다는 뜻이다. 각 프로세스는 짧게 실행됐으므로 하나의 서버 프로세스를 45분 이상 유지했을 때의 heap, private bytes, watcher 누수나 드문 1초 미만 spike까지 증명하지 않는다.

---

## 5. 초급 사용자 관점 문제점

| 문제 현상 | 어려운 이유 | 업무 영향 | 관련 코드/화면 | 개선 방법 | 난이도 | 우선순위 |
|---|---|---|---|---|---|---|
| HTTP 데이터량, HTTP 시간, TCP 측정의 차이를 한 번에 이해해야 한다 | 이름은 정확하지만 초보자에게 측정 목적과 선택 기준이 낯설다 | 부적절한 방식으로 측정하거나 결과를 잘못 비교할 수 있다 | `templates/index.html`의 `data-network-check`, README의 네트워크 체크 설명 | 각 방식 제목 아래에 “빠른 확인”, “변동 확인”, “브라우저 제외 확인” 한 줄을 유지하고 결과 간 직접 비교 금지를 강조한다 | 낮음 | P2 |
| 방화벽 확인과 TCP 클라이언트 압축 해제가 수동이다 | 포트, 인바운드 규칙, SmartScreen을 알아야 한다 | TCP 준비 실패 원인을 앱 오류로 오해할 수 있다 | README의 TCP 준비 절차, `startup_ports.check_windows_firewall_port()` | 현재 자동 변경 금지를 유지하고, 준비 상태 카드에 포트/실패 분류/다음 조치를 단계별로 표시한다 | 중간 | P1 |
| 코드서명 없는 EXE가 경고를 낼 수 있다 | 초급 사용자는 정상 배포물과 악성 경고를 구분하기 어렵다 | 실행 포기 또는 보안 경고 무시 습관이 생길 수 있다 | `README.md:28`, 릴리스 보안 산출물 | 조직 코드서명 도입 여부를 별도 결정하고, 그 전에는 SHA256 확인 절차를 첫 실행 안내에 둔다 | 높음 | P1 |
| 일반 파일 업로드는 전송량 진행률 없이 “업로드 중”만 표시한다 | 큰 파일에서 정상 전송과 멈춤을 구분하기 어렵다 | 중복 새로고침이나 업로드 중단으로 이어질 수 있다 | 업로드 form, `static/network_check.js`의 제출 잠금 | 기존 form POST를 fallback으로 유지하고 XHR 진행률을 점진적으로 추가한다 | 중간 | P1 |
| 첫 화면의 관리자 요약과 아래 작업 폼이 길다 | 처음에는 어디서 시작해야 하는지 시선이 분산될 수 있다 | 업로드 버튼과 측정 탭을 찾는 시간이 늘어난다 | `templates/index.html`, `static/style.css` | 관리자 핵심 한 줄은 유지하고 상세 표본·기술 정보의 접힘 위치를 실제 브라우저에서 검증한다 | 낮음 | P1 |
| 저장 실패나 강제 중단 실패는 진단 로그 확인이 필요하다 | 초급자는 로그 경로와 오류 종류를 해석하기 어렵다 | 재시도만 반복하거나 실패를 정상으로 오해할 수 있다 | `/api/health`, `static/operations_dashboard.js` | 관리자 카드에 안정된 오류 코드와 “서버 재시작 후 관리자에게 로그 전달” 절차를 표시한다 | 중간 | P1 |
| 실제 브라우저 접근성 동작이 미검증이다 | 정적 ARIA 검사는 실제 포커스와 읽기 순서를 보장하지 않는다 | 키보드/스크린리더 사용자가 막힐 수 있다 | `templates/index.html`, `tests/test_frontend_ux.py` | NVDA와 Edge로 탭 순서, live region 중복 안내, 그래프 키보드 탐색을 수동 확인한다 | 중간 | P1 |

### 현재 해결된 초급 사용자 문제

- `run.bat`은 `py`, `python`, 가상환경 생성 실패를 구분한다.
- 잘못된 수치·호스트·URL·경로·IP 설정은 항목과 허용 범위를 표시하고 traceback 없이 종료한다.
- 업로드와 측정 중 버튼을 비활성화해 중복 실행을 막는다.
- 대기, 진행, 부분 완료, 완료, 취소, 실패를 텍스트와 색상으로 함께 구분한다.
- 업로드·삭제·결과 저장 오류에 안정된 오류 코드, 실패 사실과 다음 조치를 표시한다.
- 전체 측정에서 먼저 끝난 방향의 결과를 보존한다.
- 첫 화면에서 접속 IP와 서버의 절대 저장 경로를 제거했다.

### 5.1 초급 사용자의 업무 활용성 명시 판정

| 평가 항목 | 현재 판정 | 코드 근거와 남은 영향 |
|---|---|---|
| 장애 원인과 현상 구분 | 부분 충족 | `build_measurement_activity()`가 시간 초과·저장·연결·인증·중단으로 원인을 분류하고 조치를 제시하지만, 원시 오류 문자열의 keyword 분류이므로 근본 원인 분석기는 아니다 |
| 장비·대상별 비교 | 기능 범위 밖 | 제품에는 장비 inventory와 batch 수집이 없다. HTTP/TCP 측정 표본만 비교하므로 장비 상태 비교 화면으로 해석하면 안 된다 |
| 이전 결과와 현재 결과 비교 | 부분 충족 | 같은 측정 방식·방향의 완료/취소/실패 전환은 표시하지만 속도 추세, 기준선, 동일 단말 보장은 없다 |
| 반복 작업 감소 | 부분 충족 | 측정 크기·시간 preset, 자동 상태 갱신, 재사용 가능한 TCP 클라이언트 ZIP은 반복 입력을 줄인다. 방화벽 확인과 ZIP 압축 해제는 여전히 수동이다 |
| 잘못된 명령·설정 적용 방지 | 충족/비해당 | 장비 명령과 장비 설정 변경 기능이 없다. 설정 파일은 범위·URL·호스트·경로·IP를 fail-closed 검증하고, 삭제는 허용 IP와 저장소 경계를 검사한다 |
| 결과를 보고 다음 조치 판단 | 부분 충족 | 실패 분류와 권장 조치는 제공하지만 성능 임계값이 없어 Mbps 값이 조직 기준에 적합한지는 사용자가 승인된 기준과 비교해야 한다 |

---

## 6. 관리자 관점 문제점

### 6.1 현재 제공되는 관리자 정보

`app.py`의 `GET /api/health`와 `GET /api/operations-summary`를 이용해 첫 화면에 다음을 표시한다.

- 서버 기능 전체 상태와 TCP 기능만 실패한 부분 장애
- 최근 측정 표본의 완료, 취소, 실패 건수
- 현재 네트워크 측정, 파일 업로드, TCP 서비스 상태
- 최근 문제 최대 5건
- 원인 분류별 영향 설명과 권장 조치
- 같은 방식·방향의 실제 완료/취소/실패 상태 전환
- 저장소, 결과 파일, 백그라운드 저장, TCP 서비스의 기술 상태
- 원본 상태 API 링크

문제가 있는 표본을 완료 표본보다 먼저 정렬하고, 30초마다 화면이 보일 때만 갱신한다. 각 요청은 10초에 중단되며 동적 문자열은 `textContent`로만 넣는다. 표본은 과거 기록이고 현재 미조치 장애가 아니라는 범위도 화면에 표시한다.

### 6.2 남은 관리자 문제

| 문제 현상 | 발생 조건 | 직접 원인 | 관리자 영향 | 관련 코드/화면 | 개선 방법 | 난이도 | 우선순위 |
|---|---|---|---|---|---|---|---|
| 건수는 장비 수가 아니라 최근 “측정 표본” 수다 | 관리자가 첫 카드의 숫자를 자산 현황으로 읽을 때 | 장비 inventory와 고유 장비 상태 모델이 없음 | 전체 장비 상태로 잘못 해석할 수 있다 | `app.operations_summary()`, `templates/index.html`의 `operations-summary-grid` | 현재 표본 문구를 유지하고 장비 수로 이름을 바꾸지 않는다 | 낮음 | P1 |
| 속도 기준값이 없어 품질 적정 여부를 판정하지 않는다 | 완료된 Mbps를 정상/장애 기준으로 사용하려 할 때 | 승인된 환경별 성능 threshold가 없음 | 완료 수치만으로 업무 기준 충족 여부를 결정할 수 없다 | `build_measurement_activity()`, 결과 설명 | 현재 “완료=저장 완료, 성능 미판정” 문구를 유지하고 기준 도입은 환경별 승인값으로 분리한다 | 중간 | P2 |
| 장애 지속 시간과 미조치 상태가 없다 | 실패 표본이 반복되거나 오래 남을 때 | incident ID, 발생/해소 시각, ack 상태 저장소가 없음 | 지금 조치가 필요한지 판단하기 어렵다 | 운영 요약 API와 CSV 구조 | 새 DB를 바로 넣지 말고, 기존 CSV를 바꾸지 않는 별도 incident/ack 파일 설계를 검토한다 | 높음 | P2 |
| 조치 이력과 담당자 기록이 없다 | 교대·보고·사후 검토가 필요할 때 | 인증된 사용자와 감사 이벤트 모델이 없음 | 장애 종료 근거와 책임 이관을 추적할 수 없다 | 현재 저장 형식에 해당 필드 없음 | 인증과 사용자 식별 정책을 먼저 정한 뒤 별도 감사 로그로 추가한다 | 높음 | P2 |
| 관리자용 PDF/인쇄 요약이 없다 | 결과를 메일·회의 자료로 그대로 공유할 때 | 운영 요약은 브라우저/API에만 있고 인쇄 layout이 없음 | 화면 캡처를 재편집해야 한다 | 화면 및 Excel은 측정별 제공 | 기존 화면 데이터를 이용한 읽기 전용 인쇄 CSS 또는 요약 내보내기를 작은 단위로 검토한다 | 중간 | P2 |
| 영향 대상은 기능 수준이고 장비·사용자 수가 아니다 | 특정 기능이 실패해 영향 규모를 산정할 때 | 개인정보 최소화를 위해 IP/PC명과 inventory를 집계하지 않음 | 장애 규모와 업무 영향도를 수량화할 수 없다 | 운영 요약은 IP/PC 이름을 의도적으로 제외 | 인증·인벤토리 정책 없이 대상 수를 추정하지 말고, 필요한 경우 별도 승인된 익명 집계 설계를 검토한다 | 높음 | P2 |
| health 저하가 HTTP 200으로 반환된다 | 외부 감시가 HTTP status만 확인할 때 | 기존 UI와 instance 감지가 JSON body의 `status`를 계약으로 사용 | 저장·백그라운드·TCP 저하를 정상 응답으로 오판할 수 있다 | `app.py`의 `/api/health`, `startup_ports.is_existing_instance()` | 기존 계약을 즉시 바꾸지 말고 모니터가 JSON `status`와 세부 check를 읽게 한다. 필요하면 별도 strict readiness endpoint를 검토한다 | 중간 | P1 |

관리자 화면의 제안 구조 중 “전체 상태 요약”, “완료/취소/실패 표본 수”, “최근 실패”, “기능 수준 영향”, “최근 상태 변경”, “권장 조치”, “원본 API 이동”은 구현됐다. “미조치 항목”, “장애 지속 시간”, “영향 대상 수”, “조치 이력”은 구현되지 않았다.

### 6.3 사용성이 떨어지는 원인과 개선 기간

| 원인 분류 | 코드에서 확인된 원인 | 단기 개선 | 중장기 개선 |
|---|---|---|---|
| 화면 구조 | 관리자 요약이 작업 탭보다 먼저 있고 모바일에서 세로 길이가 길다 | 핵심 상태는 유지하고 세부 표본·기술 영역의 접힘 위치를 브라우저에서 조정 | 역할별 첫 화면이 필요하면 사용자 연구 후 별도 진입 모드를 검토 |
| 작업 순서 | 세 HTTP/TCP 방식의 목적을 한 번에 선택해야 한다 | 각 카드의 “빠른 확인/변동 확인/브라우저 제외” 설명과 추천 순서를 유지 | 실제 초급 사용자 과업 시험으로 기본 추천 방식을 결정 |
| 전문 용어 | 포트, 방화벽, RTT, 재전송, 스트림이 노출된다 | 기본 결과에는 평균·경로·다음 조치만 두고 기술 상세에 원시 지표를 유지 | 조직 표준 용어집과 교육 문서 연결 |
| 실행 상태 | 일반 파일 form POST는 업로드 바이트 진행률이 없다 | 파일 크기·서버 제한을 제출 전에 표시하고 중복 제출 잠금을 유지 | 기존 POST fallback을 보존한 XHR 진행률 추가 |
| 오류 설명 | 안정된 오류 코드는 생겼지만 초급 사용자는 진단 로그를 직접 해석하기 어렵다 | 코드별 관리자 전달 문구와 재시도 횟수 지침 추가 | 오류 코드별 로컬 진단 묶음 내보내기 검토 |
| 결과 해석 | 성능 임계값이 없어 완료 수치의 적정 여부를 자동 판정하지 않는다 | “완료는 성능 정상 판정 아님”을 계속 표시 | 승인된 환경별 기준이 있을 때만 설정 기반 판정 추가 |
| 관리자 요약 | 최근 측정 표본만 있고 incident·담당자 모델이 없다 | 과거 표본과 현재 미조치 장애를 구분하는 문구 유지 | 인증과 감사 정책을 먼저 정한 뒤 별도 incident/ack 모델 검토 |
| 초기 설정 | 수동 방화벽과 코드서명 경고가 설치 흐름 밖에서 발생할 수 있다 | 현재 포트·수동 확인·SHA256 절차를 번호로 안내 | 조직 배포 시스템과 코드서명 도입 여부 결정 |

---

## 7. 안정성 문제점

### 7.1 예외 처리

**코드에서 확인된 사실**

- 네트워크 연결 실패와 타임아웃은 안정된 분류로 처리한다.
- 잘못된 TCP 프로토콜, 버전, 결과 필드, 수치와 합계는 실패로 종료한다.
- 빈 결과, 0바이트, 0초 또는 누락 시간은 성공으로 저장하지 않는다.
- 파일 저장 실패는 임시 파일과 부분 CSV를 되돌린다.
- 설정 인코딩, INI 형식, 수치 범위와 호스트·URL·경로·IP 의미를 검사한다.
- 손상된 CSV 중 마지막 미완성 행만 백업 후 복구하고, 헤더나 중간 손상은 시작을 중단한다.
- 일반 업로드·삭제·측정 결과 저장 예외는 rollback 뒤 안정된 오류 코드로 반환하며 raw 예외를 기본 500 화면에 노출하지 않는다.
- HTTP 시간 기준 만료, HTTP/TCP CSV 보관과 결과 저장 실패는 공통 진단 로그와 `checks.background_tasks`에 누적한다.
- HTTP 시간 기준과 TCP pending marker는 새 측정의 UUID, gate, session, watchdog과 데이터 전송 전에 503으로 차단한다. TCP의 gate 직후 경쟁은 재검사 후 gate를 반환한다.
- 제안된 웹 포트 변경을 사용자가 거절하면 `WEB_PORT_CHANGE_DECLINED`와 종료 코드 2로 서버를 시작하지 않는다.
- 측정 API와 결과 다운로드는 `no-store`와 `nosniff`로 캐시 재사용과 MIME 추측을 제한한다.
- HTTP/TCP 결과 JSON 다운로드는 권한·형식 확인과 원문 읽기를 한 번의 helper 호출로 처리한다. 보존 정리와의 삭제 경합, 잘못된 UTF-8과 JSON은 `RESULT_READ_FAILED` 500 JSON으로 반환하며 절대 경로나 raw 예외를 노출하지 않는다. 기존 404와 HTTP 시간 기준의 다른 IP 403은 유지한다.

`except Exception` 또는 `except BaseException`을 사용하는 경로는 종료·백그라운드 worker·사용자 오류 경계에 남아 있다. 현재 확인한 경로는 실패를 성공으로 바꾸지 않고 결과 상태, 안정 오류 코드 또는 진단 counter로 전환한다. 다만 새 broad catch를 추가할 때는 취소 예외와 프로세스 종료 신호를 삼키지 않는지 별도 회귀가 필요하다.

**잔여 위험**

운영체제나 백신이 파일 교체를 장시간 막는 경우 재시도 정책은 없다. 작업은 실패로 표시되며 운영자가 원인을 해소하고 다시 실행해야 한다. `result_storage.prune_old_json_results()`는 오래된 JSON 삭제 실패를 건별로 무시하므로 저장 공간 검사가 결국 저용량을 감지하더라도 보존 정리 실패의 정확한 파일과 횟수는 남지 않는다.

### 7.2 타임아웃과 재시도

| 대상 | 현재 제한 |
|---|---|
| 웹 요청 무활동 | 30초 |
| 웹 동시 요청 | 32개 |
| 종료 drain | 30초 + 강제 종료 grace 2초 |
| 빠른 HTTP 업로드 세션 | 유휴 15분, 절대 30분 |
| HTTP 시간 기준 측정 | 절대 5분 |
| TCP 측정 | 절대 5분 |
| TCP 제어 JSON | 64KiB |
| TCP 에이전트 | 만료 정리 후 256개 |
| 파일 업로드 | 4개, 합산 남은 용량 예약 |
| 운영 요약과 TCP 목록 제어 요청 | 브라우저 10초 |

HTTP 시간 기준 gate의 절대 상한은 `status()` 또는 새 `acquire()` 호출 때 평가하는 lazy 방식이다. 빠른 HTTP 업로드는 별도 timer, TCP는 watchdog이 있다. 상태 API가 주기적으로 호출되는 정상 화면에서는 취소가 발생하지만, 엄격한 독립 wall-clock 보장이 필요하면 gate 자체 watchdog이 필요하다.

재시도는 무제한 자동 반복보다 사용자가 다시 시도하는 방식을 택했다. 장비와 서버 부하를 줄이는 현재 목적에 맞는다.

### 7.3 비동기 처리와 동시성

브라우저 네트워크 호출은 비동기로 실행하고 진행 상태를 갱신한다. 실행 중 버튼을 잠그며, 서버 gate가 서로 다른 측정 방식의 중복 실행도 막는다. 세션 ID를 캡처한 취소 콜백이 이전 작업과 새 작업을 구분한다. pending marker 검사에는 storage lock을 사용하되 gate의 내부 lock을 잡은 채 보유하지 않는다. HTTP 시간 기준은 manager lock 아래 marker를 검사한 뒤 gate를 획득하고, TCP는 gate 전 검사와 획득 후 검사를 분리한다. 후자의 두 번째 검사가 실패하면 session이나 agent job을 만들지 않고 gate를 해제한다. 운영 요약은 in-flight 요청을 겹치지 않게 하고, TCP 클라이언트 목록은 화면이 숨겨졌거나 이전 요청이 남아 있으면 3초 갱신을 건너뛴다.

공유 상태는 다음 잠금으로 보호한다.

- 업로드 CSV와 캐시: `_csv_lock`
- 빠른 HTTP 측정 CSV: `_network_check_csv_lock`
- 업로드 세션: `upload_sessions_lock`
- HTTP 시간 기준 관리자: `SustainedCheckManager.lock`, `storage_lock`
- TCP 서비스: 서비스 내부 lock과 storage lock
- 서버 전체 측정: `NetworkMeasurementGate`

### 7.4 자원 정리

- 파일은 `with` 문과 원자적 임시 파일 패턴을 사용한다.
- TCP 세션과 소켓은 정상, 실패, 취소, 타임아웃, 서버 종료 경로에서 정리한다.
- 완료된 TCP 세션과 소켓 참조는 개수와 TTL로 제한한다.
- 임시 업로드 파일은 소유 프로세스 생존 여부와 나이를 확인하고 정리한다.
- 프로세스 종료 뒤 운영체제 잠금을 검증하는 subprocess 테스트가 있다.
- 실제 Windows soak에서 386 cycles의 initial/restart 총 772개 프로세스와 1,544개 표본을 수집했고 handle·thread·TCP socket의 final 상태가 반복 증가하지 않았다.

### 7.5 데이터 정확성과 무결성

- 업로드 CSV 전체 재작성과 JSON은 임시 파일, `fsync`, 원자적 교체를 사용한다. 측정 CSV append는 행마다 `fsync`하고 일반 예외에서는 `rollback_requested` marker 아래 해당 session 행을 원자 재작성으로 제거한다.
- Windows 교체는 `MoveFileExW`의 replace 및 write-through 플래그를 사용한다.
- CSV가 10,000행을 넘으면 최근 5,000행을 남기고 월별 archive에 중복 없이 보관한다.
- JSON 상세 결과는 최신 1,000건으로 제한한다.
- 부분 저장 결과는 `status=failure` 또는 `cancelled`로 구분한다.
- 완료 전 결과 URL을 공개하지 않는다.
- Excel의 KST 변환과 수식 주입 방지를 테스트한다.
- CSV의 `checked_at`과 JSON의 시작·완료 시각은 `datetime.now().astimezone()`의 UTC offset을 포함한다. 운영 요약의 브라우저 표시와 Excel은 이 값을 기준으로 하며, 다른 시간대 PC와 DST 경계의 직접 왕복 시험은 아직 없다.

Excel 생성기는 `=`, `+`, `-`, `@`로 시작하는 문자열을 중화하지만 운영 CSV 자체는 호환성 유지를 위해 원문을 저장한다. 따라서 업로드 메모·파일명이나 상세 측정 오류가 들어간 원본 CSV를 스프레드시트에서 직접 열 때는 formula 실행 가능성을 별도 보안 경계로 취급해야 한다.

**코드에서 확인된 사실**

HTTP 시간 기준과 TCP 상세 JSON·CSV는 `measurement_transactions.py`의 durable intent로 묶는다. 재시작은 `(session_id, direction)` 또는 `(session_id, phase)`별 누락 행만 복구하고 JSON semantic hash, JSON에서 다시 생성한 예상 행, marker 행과 현재 CSV의 모든 필드가 정확히 맞는지 검사한다. 일반 예외 rollback은 `rollback_requested` 상태로 방향을 고정하며 부분 정리는 다음 시작에서 완료한다. marker 없는 과거 고아 JSON이나 운영자가 수동으로 바꾼 파일은 자동 추정하지 않는 것이 남은 의도적 제한이다.

### 7.6 로그와 장애 추적

로그에는 시간, 로거 이름, 단계 이벤트, 오류 종류가 있다. 업로드 파일명, 메모, 인증 토큰, 전송 내용과 raw 예외 메시지는 새 저장 실패 이벤트에 기록하지 않는다. 관리자 요약은 원시 오류를 그대로 내보내지 않는다. 보관이나 백그라운드 저장 실패 누적은 health API와 첫 화면 기술 상태에서 확인할 수 있다. 반면 상세 측정 JSON과 운영 CSV는 문제 재현을 위해 제한 길이의 client/transfer 오류를 보존할 수 있으므로 관리자 요약과 같은 비민감 진단 자료로 간주하면 안 된다.

남은 한계는 로그가 단일 서버 파일이고 일부 트랜잭션 best-effort helper가 모듈 logger에 의존한다는 점이다. 장애 이력과 운영자 조치 이력은 별도 구조가 없다.

### 7.7 안정성 세부 항목의 명시 판정

| 점검 항목 | 판정 | 근거 또는 남은 위험 |
|---|---|---|
| 권한 부족 | 부분 방어 | 시작 시 저장 폴더 준비 실패는 안정 오류 코드로 종료하고, 실행 중 쓰기 실패는 rollback·실패 응답 또는 health 저하로 남긴다. 새 EXE의 일반 사용자·보호 폴더 실행은 미검증이다 |
| 한글 인코딩 | 코드 방어/패키지 미검증 | 설정은 UTF-8, CSV는 UTF-8 BOM, Excel 문자열 방어 테스트가 있다. 한글 Windows 경로의 새 EXE 실행은 필요하다 |
| 외부 프로세스 실행 실패 | 런타임 비해당, 설치 경로 검증 | 서버 런타임은 ping·장비 CLI 같은 외부 프로세스를 실행하지 않는다. `run.bat`의 Python/venv/pip 실패 분기는 테스트하지만 백신·조직 정책별 실패는 실제 PC에서 확인해야 한다 |
| 타이머·작업 스레드 중복 | 구조와 반복 재기동 시험에서 제한 | `ProbeService.start()`는 `started`로 accept thread 중복을 막고, 측정 gate와 session ID가 watchdog의 이전 세션 개입을 막는다. 실제 2,708.89초 soak에서 772개 프로세스의 thread final 값이 안정적이었지만 각 프로세스가 짧아 단일 장기 프로세스의 daemon watcher 누수는 별도 시험이 필요하다 |
| 이전·현재 결과 오비교 | 범위 제한 | 상태 전환은 같은 `source`와 `direction`만 연결한다. 단말 identity와 속도 baseline이 없으므로 “같은 장비의 성능 변화”로 표시하지 않는다 |
| 정렬·필터 중 원본 손실 | 읽기 경로 없음 | 운영 요약은 CSV 행을 복사해 정렬하며 원본을 다시 쓰지 않는다. CSV compact는 이전 행을 월별 archive에 원자 저장한 뒤 활성 파일을 줄인다. JSON 1,000건 제한은 의도된 보존 정책이다 |
| 동일 오류 로그 폭증 | 디스크 상한은 있음, rate limit 없음 | 로그는 2MiB×6개로 순환하지만 지속 실패 이벤트별 rate limit은 없다. 반복 실패 횟수와 마지막 이벤트는 health에 누적되므로 장시간 fault 시험에서 로그량도 계측해야 한다 |
| 일부 실패의 정상 오판 | 방어 | 빠른/시간 기준/TCP 결과의 주 저장 실패는 성공으로 반환하지 않는다. CSV archive 같은 보조 저장 실패는 주 결과를 유지하되 health를 degraded로 표시한다 |

### 7.8 코드 품질과 유지보수성

| 점검 항목 | 코드에서 확인된 사실 | 저위험 개선 방향 |
|---|---|---|
| 역할 과다 | `app.py` 3,059줄이 설정 적용, Flask route, 파일·CSV 처리, 빠른 측정, 운영 요약, 프로세스 시작·종료를 함께 담당한다. `ProbeService` 1,581줄도 listener, agent, session, TCP worker, 검증, 저장을 함께 담당한다 | 동작을 바꾸지 않고 먼저 운영 요약 builder, 오류 payload 같은 순수 helper만 별도 모듈로 이동. 측정 transaction은 이미 공통 모듈로 분리했다 |
| UI·통신·저장 결합 | UI 파일은 분리됐지만 `create_app()` 내부 closure가 세션 상태, route, CSV 저장과 종료 callback을 직접 연결한다 | 기존 URL과 payload를 고정하는 contract test를 유지하면서 blueprint 단위로 한 경계씩 분리 |
| 중복 코드 | 공통 측정 transaction helper와 source별 결과→CSV 행 builder는 분리됐다. archive/prune 호출과 진단 failure counter 정책은 두 manager에 각각 남아 있다 | 기존 CSV field와 오류 문구를 유지하면서 archive/prune·진단 adapter만 작은 단위로 공통화할지 회귀 근거를 먼저 만든다 |
| 전역 상태 의존 | `APP_ROOT`, 큰 1MiB `NETWORK_CHECK_CHUNK`, CSV lock 두 개는 모듈 전역이다. 반면 세션·gate·manager는 대부분 app instance에 귀속된다 | 테스트 injection을 유지하고 전역 lock을 당장 합치지 않는다. 큰 상수 데이터는 메모리·성능 측정 뒤에만 변경 |
| 하드코딩·매직 넘버 | 중요한 상한은 named constant 또는 settings dataclass로 모였으나, UI 갱신 간격과 일부 join/grace 값은 구현 파일에 직접 남는다 | 호환 동작을 먼저 테스트로 고정한 뒤 운영 조정 가능성이 있는 값만 명명·문서화 |
| 입력·반환 계약 | dataclass와 type hint를 넓게 쓰지만 route와 운영 요약은 자유 형식 `dict[str, Any]`를 수동 조립한다 | 새 framework 없이 TypedDict 또는 작은 serializer helper로 필수 key를 검증 |
| 타입·데이터 일관성 | JSON은 수치형, CSV reader는 문자열이며 `build_measurement_activity()`가 수동 변환·범위 검사를 한다 | 변환 helper의 fixture를 늘리고 원본 저장 형식은 바꾸지 않는다 |
| 변경 파급 | field list, 상태 문자열과 오류 분류가 저장기·API·JS·Excel·테스트에 걸쳐 연결된다 | 저장/API 호환 contract test를 변경 전 먼저 추가하고 한 계층씩 수정 |
| 테스트 난이도 | clock, logger, gate, 임시 root 주입은 좋다. 반면 `create_app()`의 중첩 함수와 Windows `main()` 종료 경로는 직접 단위 테스트가 어렵다 | 현재 Flask client와 subprocess fault test를 유지하며 순수 helper 추출만 진행 |

### 7.9 Windows soak 분석 결과와 판정 계약

**실행으로 확인된 사실**

| 항목 | 결과 |
|---|---:|
| 실행 시간 | 2,708.89초 |
| 완료 cycles | 386 |
| 업로드 bytes | 101,187,584 |
| TCP 자체 점검 | 386 |
| 프로세스 / 표본 | 772 / 1,544 |
| 자원 계측 | working set, handle, thread, TCP socket 모두 `available` |
| 분석 품질 | `pass`, issues 0 |
| 자원 findings | 0 |
| 최종 판정 | `PASS_NO_REPEATED_PROCESS_GROWTH` |

working set은 initial 기준 54,228,992 bytes에서 최종 55,867,392 bytes로 1,638,400 bytes 증가했고, restart 기준 56,008,704 bytes에서 최종 57,176,064 bytes로 1,167,360 bytes 증가했다. 둘 다 review 절대 기준 16,777,216 bytes보다 작았다. handle, thread와 TCP socket final 값도 반복 증가 패턴이 없었다.

`tools/analyze_windows_soak_summary.py`는 판정 전에 네 계약을 검사한다.

1. **duration 계약**: 기본 45분 이상인지 확인한다.
2. **기능 계약**: 완료 cycles와 예상 업로드 bytes, TCP 자체 점검 횟수가 일치하는지 확인한다.
3. **구조 계약**: initial/restart PID, 표본 수, 지표 필드, 상태와 전체 coverage를 확인한다.
4. **tail 계약**: 마지막 분석 구간에서 각 지표가 완전하게 수집됐는지 확인한다.

품질을 통과한 표본에 대해 `repeated_process_growth`, `persistent_level_shift`, `strong_peak_excursion`, `within_pid_increase` 네 anomaly를 검사한다. CLI 종료 코드는 0=`PASS_NO_REPEATED_PROCESS_GROWTH`, 1=`REVIEW_RESOURCE_ANOMALY`, 2=`INCONCLUSIVE_TELEMETRY` 또는 입력/출력 오류, 3=`FUNCTIONAL_FAIL`이다.

**해석 제한**

각 cycle의 initial과 restart는 다른 PID이며 프로세스별 표본은 시작·종료 중심이다. 따라서 이 PASS는 반복 재기동과 기능 수행 중 높은 상태가 누적되지 않았다는 근거다. 단일 프로세스 장시간 heap/private bytes 누수, 1초 미만 spike, 실제 대용량 사용자 전송을 증명하지 않는다.

---

## 8. P0, P1, P2 우선순위 개선 목록

상태가 “완료”인 항목은 이번 작업트리에 반영됐다. “잔여”는 후속 변경 후보를 뜻한다.

| 우선순위 | 구분 | 문제 | 발생 조건 | 사용자 영향 | 원인 | 관련 코드 | 개선 방법 | 검증 방법 | 예상 변경 범위 |
|---|---|---|---|---|---|---|---|---|---|
| P0 | 완료, 데이터 | 파일 확정과 CSV 기록 분리 | 두 단계 사이 프로세스 종료 | 파일 또는 기록 유실, 잘못된 삭제 | 기존 원인: 트랜잭션 부재(해소) | `upload_transactions.py`, `app.recover_upload_transactions()` | durable marker와 시작 복구 적용 완료 | `test_fault_injection.py`, `test_upload_transaction_recovery.py` | 저장 계층, 업로드/삭제 |
| P0 | 완료, 정확성 | 0바이트나 잘못된 합계를 성공으로 저장 | 빈 결과, 형식 변경, 중단 | 잘못된 정상 판단 | 성공 조건 부족 | `network_sustained.py`, `network_probe/service.py` | 양수 전송량/시간과 합계/범위 검증 | sustained/probe 정상 및 실패 테스트 | 측정 검증 |
| P0 | 완료, 종료 | 종료되지 않는 요청 뒤 데이터 잠금 해제 | buffered read나 멈춘 핸들러 | 두 프로세스 동시 기록, CSV 손상 | 정상 종료만 가정 | `bounded_server.py`, `app.main()` | drain, 소켓 종료, grace, lock 유지 hard exit | `test_bounded_server.py`, `test_startup_ports.py` | 서버 종료 |
| P0 | 완료, 동시성 | 만료 콜백이 새 세션을 닫거나 gate를 일찍 해제 | 지연 콜백, 저장 실패 | 겹친 측정, 결과 오염 | 세션 경계 부족 | `network_measurement.py`, 각 관리자 | 세션 ID/owner 일치 확인, 소켓 정리와 결과 저장 시도 뒤 release | gate/sustained/probe persistence 대기 테스트 | 측정 관리자 |
| P0 | 완료, 시작 | 손상 설정/트랜잭션이 traceback으로 종료 | 잘못된 INI, UTF-8, marker | 초급 사용자가 원인 파악 불가 | 시작 예외 누락 | `startup_ports.py`, `app.run_smoke_check()`, `app.main()` | fail-closed 한국어 안내와 진단 이벤트 | startup/smoke 테스트 | 시작 경로 |
| P1 | 완료, 사용성 | 실행 중/완료/실패 구분과 다음 조치 부족 | 느린 네트워크, 부분 실패 | 중복 클릭, 결과 오해 | 상태 모델 부족 | `templates/index.html`, `static/*.js` | 버튼 잠금, 텍스트 상태, 진행률, guidance | `test_frontend_ux.py` | UI |
| P1 | 완료, 오류 처리 | 업로드·삭제·측정 저장 실패가 기본 500 또는 raw 예외로 노출 | 디스크·권한·CSV 오류 | 실패 원인과 조치 불명확 | 사용자/진단 경계 부족 | `app.py`, `network_sustained.py`, `network_probe/service.py` | rollback 후 안정 오류 코드와 안전한 진단 이벤트 반환 | 저장 실패, raw 문구 비노출, gate 해제 테스트 | 저장 및 HTTP 응답 |
| P1 | 완료, 진단 | expiry·CSV archive 실패가 조용히 사라짐 | 백그라운드 만료·보관 오류 | 저장소 증가와 누락 추적 불가 | `pass`와 stderr 의존 | sustained/probe manager, `/api/health` | 공통 logger와 failure counter, health degraded 추가 | archive/expiry fault tests | 진단 및 health |
| P1 | 완료, 설정 | URL·경로·삭제 IP 의미 검증 부족 | 위험한 BASE_URL, 빈 STORAGE_ROOT, CIDR | 잘못된 링크, 시작 실패, 삭제 오판 | 수치 validator만 존재 | `startup_ports.py`, `load_config()` | 형식·의미 검증과 안전한 시작 실패 | parameterized config/main tests | 설정 경계 |
| P1 | 완료, 관리자 | 완료를 정상 품질, TCP 단독 실패를 전체 장애로 오해 | 기준값 없음, probe 불가 | 관리자 오판 | 상태 용어와 기능 영향 혼합 | `build_measurement_activity()`, `operations_dashboard.js` | 완료/취소/실패, 성능 미판정, 부분 장애로 분리 | API/정적 UI 테스트 | 읽기 전용 API/UI |
| P1 | 완료, 브라우저 안정성 | 운영 API와 TCP 목록 요청이 끝없이 대기하거나 겹침 | 느린 서버, 숨겨진 탭 | 로딩 고착, 서버 부하, 선택 불안정 | client timeout/in-flight guard 부재 | `operations_dashboard.js`, `network_probe.js` | 10초 AbortController, visibility와 중복 요청 검사 | JS 구조/문법과 장애 재현 테스트 | 프런트엔드 |
| P0 | 완료, 데이터 | 측정 JSON 확정과 CSV append 사이 강제 종료 | 두 저장 단계 사이 process kill | 상세만 존재하거나 요약 행 누락으로 데이터 손실·오판 가능 | 기존 원인: 측정 journal/reconciliation 부재(정의한 kill 경계에서 해소) | `measurement_transactions.py`, 두 측정 저장기, `app.ensure_directories()` | durable intent, JSON hash, key별 idempotent 시작 재조정 적용 완료 | HTTP/TCP JSON 직후·full 첫 CSV 행 직후 4개 kill test | 측정 저장 계층 |
| P1 | 완료, 데이터 | marker 행·JSON 의미 불일치, rollback 부분 실패와 pending 상태의 후속 측정 | marker 손상, CSV 정리 또는 JSON 삭제 단독 실패, marker cleanup 실패 | 잘못된 요약 복구, 불필요한 재측정 트래픽 또는 다음 시작 중단 | 기존 원인: marker 행 재생성·rollback 상태와 시작 전 차단 부재(해소) | `measurement_transactions.py`, `network_sustained.py`, `network_probe/service.py` | JSON에서 행 재생성·전체 대조, durable `rollback_requested`, 시작 전 503 차단, TCP gate 경쟁 시 해제와 복구 오류 보존 적용 완료 | semantic mismatch, 두 부분 rollback, gate/session/watchdog 미실행, gate race release, 오류 코드 보존 | 측정 저장 계층과 시작 경로 |
| P1 | 완료, 장시간 검증 | 재시작 반복 중 자원 증가 여부가 추측에 머묾 | 업로드·kill·restart·TCP 반복 | 누수나 핸들 증가를 배포 전에 놓칠 수 있음 | 짧은 1 cycle만 존재했던 검증 공백(해소) | `tools/run_windows_stability_soak.py`, `tools/analyze_windows_soak_summary.py` | 2,708.89초, 386 cycles, 772 processes/1,544 samples 계측과 품질 gate·4종 anomaly 판정 완료 | quality pass/issues 0/findings 0, `PASS_NO_REPEATED_PROCESS_GROWTH` | 테스트·CI 분석 |
| P1 | 완료, 결과 다운로드 | JSON 검증 뒤 재독 사이 삭제 경합이 HTML 500과 절대경로 traceback으로 노출 | 1,000개 초과 결과 보존 정리와 다운로드가 겹치거나 파일이 잠김·삭제됨 | 초급 사용자가 개발자 오류를 보고 결과를 받지 못함 | 존재·권한 검증과 응답 읽기가 두 번의 파일 접근으로 분리됨 | `network_sustained.py`, `network_probe/service.py`, `network_probe/routes.py` | 원문을 한 번 읽어 JSON·소유자를 검증하고 `OSError`, `UnicodeError`, 손상 JSON을 `RESULT_READ_FAILED`로 변환 | 삭제 경합, invalid UTF-8, 단일 읽기, 404/403·헤더 회귀 | 결과 조회 helper와 두 JSON route |
| P1 | 진행 중, 배포 게이트 | `v0.5.1` 소스와 게시 asset의 동일 commit 여부를 아직 확인해야 함 | 기존 v0.4.6 설치 또는 checkout이 tag ref를 평탄화 | 개선 전 동작 사용 또는 tag/source 불일치 | 원격 tag object 재조회 없는 immutable 검사 | `app_version.py`, `build_windows_release.ps1`, `release.yml` | 원격 tag ref를 force-fetch하고 clean commit의 annotated tag만 `--verify-tag`로 게시 | ZIP verifier, 3개 EXE 자체 점검, manifest commit, SHA256와 Release asset 대조 | 빌드/릴리스 |
| P1 | 잔여, 보안 | 인증/TLS 없이 사내망에 노출되고 TCP token이 평문 제어 채널을 통과 | 신뢰하지 않는 단말이나 도청 가능한 구간이 같은 망에 존재 | 무단 업로드·부하, token 재사용 가능성 | trusted-LAN 전제의 초기 도구 범위 | Flask 라우트 전체, TCP agent API | 우선 망 분리·접근 통제, 인증/TLS는 기존 URL과 client 호환성을 포함한 별도 버전으로 설계 | 보안 검토, 패킷 노출과 token 재사용 시험 | 아키텍처와 배포 |
| P1 | 잔여, 운영자료 | 추적 runtime template에 운영값이 기록된 뒤 Git stage 가능 | 같은 clone에서 운영 후 `git add -A` | 내부 주소·기록의 우발적 커밋 | header template 네 파일을 의도적으로 추적 | `config.ini`, `data/*.csv`, `tests/test_app.py` | header-only/default 자동 test를 유지하고 stage 전 파일 확인을 릴리스 체크리스트에 둔다 | repository template test, `git diff --cached` 점검 | 저장소 운영 절차 |
| P1 | 잔여, 현장 UX | 실제 Edge/Chrome/Android/스크린리더 미검증 | 현장 브라우저 사용 | 레이아웃/포커스 문제 누락 가능 | Computer Use URL 안전 확인 3회 연속 실패 | `templates`, `static` | 민감정보 없는 수동 체크리스트로 현장 검증 | 브라우저별 캡처와 NVDA 기록 | 검증 문서, 소규모 UI |
| P1 | 잔여, 초급 UX | 일반 파일 업로드의 바이트 진행률이 없음 | 대용량 파일·느린 회선 | 멈춤으로 오해하고 새로고침 | 일반 form POST | 업로드 form, `network_check.js` | 기존 POST fallback을 유지한 점진적 XHR 진행률 | 중단·뒤로가기·대용량 브라우저 시험 | 업로드 UI |
| P2 | 잔여, 관리자 | 미조치와 조치 이력 없음 | 반복 장애 | 추적과 보고 어려움 | 사용자/incident 모델 없음 | 현재 CSV 구조 | 기존 CSV를 건드리지 않는 별도 읽기/기록 모델 검토 | 이력 무결성/권한 시험 | 신규 독립 모듈 |
| P2 | 잔여, 보고 | 관리자 공유용 인쇄/요약 파일 없음 | 상급자 보고 | 화면 캡처에 의존 | 측정별 Excel만 존재 | 대시보드 | 읽기 전용 인쇄 CSS 또는 요약 export | 민감정보/페이지 QA | UI/보고 |
| P2 | 잔여, 보안 | 요청 Host 기반 TCP client URL | trusted proxy 밖의 유효하지만 잘못된 Host로 ZIP 요청 | client가 잘못된 서버에 연결 | request host 우선 자동 구성 | `network_probe/client_package.py`, `network_probe/routes.py` | trusted-host 또는 승인된 URL 정책을 기존 자동 감지와 호환되게 별도 설계 | Host 변형, proxy, loopback fallback 테스트 | 클라이언트 패키지 경계 |
| P2 | 잔여, 데이터 보안 | 상세 오류·원본 CSV의 formula와 민감정보 가능성 | 운영 CSV를 Excel로 직접 열거나 client 오류에 내부 정보 포함 | 수식 실행 또는 운영정보 노출 | 원본 보존과 CSV 호환성 우선 | `app.py`, 두 측정 CSV builder, 상세 JSON | 안전한 Excel 내보내기를 우선 안내하고 raw export를 민감 자료로 분류. 저장 형식 변경은 별도 호환성 검토 | `=+-@` fixture, raw/summary 노출 경계 테스트 | 문서·내보내기 경계 |
| P2 | 잔여, 모니터링 | health degraded가 HTTP 200 | 외부 감시가 status code만 확인 | 부분 장애 누락 | 기존 UI와 instance 감지의 JSON body 계약 | `app.py`, `startup_ports.py` | 모니터가 body를 읽게 하고 필요 시 별도 strict readiness endpoint 검토 | degraded body/status 및 기존 instance 회귀 | API 추가 또는 운영 설정 |
| P2 | 잔여, 유지보수 | `app.py` 3,059줄, `ProbeService` 1,581줄 | 업로드나 TCP API 변경 | 회귀 영향 범위 증가 | 기능 집중 | `app.py`, `network_probe/service.py` | 먼저 순수 helper와 blueprint만 작은 단위로 이동 | 현재 전체 회귀 458건 유지 | 단계적 모듈 분리 |
| P2 | 잔여, 보존 | 오래된 JSON 삭제 실패가 건별 기록되지 않음 | 파일 잠금·백신 | 보존 상한 초과와 디스크 증가 | cleanup이 unlink 오류를 계속 처리 | `result_storage.py:29-52` | 실패 횟수만 진단 counter에 연결하고 주 결과는 성공 유지 | 잠긴 파일 prune test, health warning | 결과 보존 |
| P2 | 잔여, 무결성 | 외부 수동 교체 업로드 파일의 소유권 판별 불가 | 비정상 종료 뒤 CSV 없이 같은 경로 교체 | 잘못된 복구 가능성 | 업로드 marker에 파일 identity 없음 | `upload_transactions.py` schema v1 | 다음 업로드 schema에서 크기/hash 또는 독점 소유 표식 검토 | 교체/충돌 fault test | 업로드 transaction v2, 마이그레이션 |

전체 회귀 458건과 fault suite 32건, 정의한 네 개 측정 process-kill 경계, 실제 2,708.89초 Windows soak 범위에서는 즉시 재현되는 P0가 없다. 모든 저장 실패 조합, 실제 전원 차단, 단일 프로세스 장기 실행, 배포 EXE와 현장 환경까지 P0가 없음을 증명한 것은 아니다.

---

## 9. 단계별 개선 계획

### 1단계: 치명적 안정성 문제 해결

- **작업 목적**: 비정상 종료, 무한 대기, 데이터 손실, 잘못된 성공 판정, 잠금 누수를 막는다.
- **변경 대상**: `app.py`, `bounded_server.py`, `network_measurement.py`, `network_sustained.py`, `network_probe`, `runtime_stability.py`, `upload_transactions.py`, `measurement_transactions.py`
- **예상 위험**: 정상 업로드와 기존 결과 형식을 건드릴 수 있다.
- **기존 기능 영향**: URL, CSV, JSON, Excel, TCP 프로토콜 v2는 유지한다.
- **필요한 테스트**: 프로세스 강제 종료, 부분 CSV 쓰기, 0바이트, 잘못된 시간/합계, 만료 콜백, 종료 lock
- **완료 기준**: split-brain 자동 복구 또는 fail-closed, 성공 조건 무결성, 남은 스레드에서 lock 유지, 측정 JSON-CSV kill 경계 검증
- **현재 상태**: 전체 회귀 458건과 fault suite 32건 범위에서 업로드·삭제·측정 저장, 종료, 성공 판정 P0의 저위험 방어를 완료했다. pending marker는 시작 전 503으로 차단하고 TCP gate 경쟁도 회수한다. 실제 전원 차단과 새 배포 EXE는 별도 검증이 필요하다.

### 2단계: 사용자 오류 방지

- **작업 목적**: 설정과 입력 실수를 시작 전에 잡고 실패를 사용자가 알아보게 한다.
- **변경 대상**: `startup_ports.py`, `run.bat`, 업로드 검증, 각 측정 UI
- **예상 위험**: 과거에 묵인한 잘못된 설정이 더 이상 실행되지 않을 수 있다.
- **기존 기능 영향**: 유효한 기존 설정과 포트 변경 흐름은 유지한다.
- **필요한 테스트**: 잘못된 숫자/불리언/UTF-8/INI/URL/경로/IP, 중복 클릭, 500/503/507, 부분 실패
- **완료 기준**: traceback과 임의 fallback 없이 항목/허용 범위/다음 조치 제공
- **현재 상태**: 자동 테스트 범위 완료. 실제 Release EXE의 권한·한글 경로 실패 문구는 미검증

### 3단계: 초급 사용자 사용성 개선

- **작업 목적**: 작업 순서, 현재 상태, 결과 의미, 다음 조치를 분명히 한다.
- **변경 대상**: `templates/index.html`, `static/network_check.js`, `network_sustained.js`, `network_probe.js`, `style.css`
- **예상 위험**: 기존 DOM selector나 자동 측정 흐름이 깨질 수 있다.
- **기존 기능 영향**: 기존 form 필드, URL, 측정 순서는 유지한다.
- **필요한 테스트**: accessible tab/progress/status, 버튼 잠금, 부분 결과, 키보드 탐색
- **완료 기준**: 대기/진행/완료/부분 완료/실패 구분, 오류와 다음 조치 표시
- **현재 상태**: 소스와 정적 테스트 완료. 일반 파일 업로드 진행률과 실제 브라우저/스크린리더 검증 필요

### 4단계: 관리자용 정보 개선

- **작업 목적**: 첫 화면에서 운영 상태와 조치 필요 여부를 판단하게 한다.
- **변경 대상**: `/api/health`, `/api/operations-summary`, 관리자 대시보드 UI
- **예상 위험**: 표본을 장비 수나 네트워크 품질 판정으로 오해할 수 있다.
- **기존 기능 영향**: 기존 CSV/JSON을 읽기만 하며 저장 형식을 바꾸지 않는다.
- **필요한 테스트**: 문제 우선 정렬, 민감정보 제외, 일부 원본 손상, API 일부 실패, 자동 갱신 상한
- **완료 기준**: 서버 기능 상태, 표본 수, 최근 문제, 실제 상태 전환, 기능 영향, 권장 조치, 기술 상세 이동 제공
- **현재 상태**: 최근 측정 표본 요약 범위에서 구현과 자동 테스트 완료. 장비 현황, 미조치·조치 이력, 지속 시간과 공유 보고서는 후속 P2

### 5단계: 회귀 테스트와 장시간 안정성 검증

- **작업 목적**: 기능 호환성과 장시간 자원 정리를 검증한다.
- **변경 대상**: `tests`, `tools/run_stability_fault_suite.py`, `tools/run_windows_stability_soak.py`, GitHub Actions
- **예상 위험**: 45분 재시작 반복 soak도 단일 프로세스 장기 실행과 실제 현장 부하의 메모리 누수를 충분히 드러내지 못한다.
- **기존 기능 영향**: 테스트는 임시 폴더와 로컬 루프백만 사용한다.
- **필요한 테스트**: 전체 회귀, 장애 주입, 45분 반복, 대량 출력, 실제 브라우저, 실제 배포 EXE
- **완료 기준**: 자동 회귀와 fault suite, 45분 자원 계측 soak, 브라우저/EXE/저권한/한글 경로 검증
- **현재 상태**: 전체 회귀 458건, fault 32건, Python `compileall`, 5개 JavaScript 구문 검사와 diff-check를 통과했다. 실제 Windows soak는 2,708.89초, 386 cycles, 772 processes/1,544 samples로 완료했으며 analyzer quality pass, issues 0, findings 0, `PASS_NO_REPEATED_PROCESS_GROWTH`였다. 단일 프로세스 장시간 실행, 실제 브라우저와 새 EXE 검증은 미완료다.

---

## 10. 테스트 및 검증 계획

### 10.1 요구된 테스트의 현재 상태

| 테스트 | 현재 근거 | 판단 |
|---|---|---|
| 정상 연결 | TCP self-check, full probe session, Flask API | 자동 검증됨 |
| 연결 실패 | TCP preflight와 stable error category 테스트 | 자동 검증됨 |
| 인증 실패 | 웹 사용자 인증은 없음. TCP API의 Bearer/token 401, 잘못된 클라이언트 토큰과 버전 거절 테스트는 존재 | TCP 자동 검증, 웹 인증은 기능 자체가 없음 |
| 타임아웃 | HTTP 세션, TCP attach/job/result, gate max-hold | 자동 검증됨 |
| 장비 응답 없음 | TCP unclaimed job/result timeout | 자동 검증됨 |
| 변경된 출력 형식 | Probe 결과 allowlist와 수치/배열 검증 | 자동 검증됨 |
| 빈 결과 | 0바이트, 빈 results, 누락 duration | 자동 검증됨 |
| 대량 출력 | TCP 제어 JSON 64KiB와 배열/스트림 상한 | 제어 데이터 경계만 자동 검증, 실제 고속·대량 전송 미검증 |
| 끝없이 출력되는 명령 중단 | 외부 장비 명령은 없음. TCP/HTTP 절대 상한 적용 | 해당 기능 없음/상한 검증 |
| 여러 장비 동시 수집 | 이 제품은 장비 배치 수집기가 아니다. 등록 agent 상한 256, 실제 측정은 전역 1건 | 기능 비해당, agent cap 구조 검증 |
| 일부 장비 실패 | 배치 장비 개념은 없다. 한 측정/API source 실패가 다른 운영 요약 source를 숨기지 않는지 검사 | 제품 범위에 맞춘 실패 격리 자동 검증 |
| 중복 데이터 | CSV archive 재시도 dedupe, 트랜잭션 idempotency | 자동 검증됨 |
| 프로그램 강제 종료 | 업로드 파일 확정, 삭제 CSV 갱신, HTTP/TCP JSON 확정, full 첫 CSV 행 확정 직후 실제 subprocess kill | 6개 저장 경계 자동 복구와 exactly-once 검증 |
| 작업 취소 | 빠른 HTTP, sustained, TCP 취소 | 자동 검증됨 |
| 파일 저장 실패 | 디스크 부족, replace, 부분 CSV/JSON write, safe 오류 코드, background health, 측정 durable marker와 `rollback_requested` | 일반 예외, CSV/JSON 단독 rollback 실패와 process-kill 경계 자동 검증 |
| 한글/특수문자 인코딩 | UTF-8 BOM CSV, 비 UTF-8 설정 거절, Excel KST/수식 방어 | 저장 형식은 부분 자동 검증. 실제 한글 파일명·메모·하위 폴더 왕복과 한글 Windows 경로 EXE는 미검증 |
| 장시간 실행 | 주간 45분 workflow와 실제 2,708.89초, 386-cycle Windows 실행 | 반복 재기동·업로드·다운로드·TCP 기능과 자원 계측 자동 검증. 단일 프로세스 장기 실행은 별도 |
| 메모리/세션 누수 | terminal session cap/TTL, 소켓 참조 해제, 772 processes/1,544 samples의 working set/handle/thread/TCP socket | `PASS_NO_REPEATED_PROCESS_GROWTH`; 반복 프로세스 추세는 자동 판정됨. 단일 프로세스 heap/private bytes 장기 누수는 미증명 |
| pending 측정 복구 | HTTP/TCP start 전 marker 검사, TCP gate 후 재검사, 저장 직전 오류 보존 | 503, gate/session/watchdog/job 미생성, 경쟁 시 gate release와 오류 코드 보존 자동 검증 |
| API 캐시·MIME 방어 | HTTP 시간 기준/TCP blueprint와 결과 다운로드의 `no-store`, `nosniff` | 정상 및 오류 응답 헤더 자동 검증 |
| 결과 JSON 읽기 경합·인코딩 | HTTP/TCP 결과 단일 읽기, 보존 정리 사이 삭제, invalid UTF-8 | 500 JSON `RESULT_READ_FAILED`, 경로·예외 비노출, 기존 404/403 자동 검증 |
| repository 예시 데이터 | 추적 `config.ini` 기본값과 네 운영 CSV header-only 검사 | 자동 검증됨. 사용자의 강제 stage 자체는 차단하지 않음 |
| 기존 기능 회귀 | 전체 458 tests, fault suite 32 tests, Python compileall, 5개 JavaScript 구문 검사와 diff-check | 자동 검증됨 |

### 10.2 실제 장비 없이 검증하는 방법

1. `FakeClock`으로 5분, 15분, 30분을 즉시 진행한다.
2. Flask test client로 사용자 IP, 요청 본문, 파일 업로드, 오류 상태를 재현한다.
3. 임시 폴더와 fault injection으로 디스크 및 CSV/JSON 실패를 만든다.
4. 로컬 루프백 TCP 서버와 self-check로 외부 장비 없이 전송을 검증한다.
5. 하위 Python 프로세스를 kill해 전원 차단과 비슷한 중단 지점을 만든다.
6. 정상/비정상 TCP 결과 fixture로 클라이언트 출력 형식 변경을 재현한다.
7. 운영 자료 대신 생성한 비민감 텍스트와 바이트 패턴만 사용한다.
8. soak JSON 분석기는 duration, 기능 횟수, 프로세스·표본 구조, 전체·tail coverage를 먼저 검증하고 품질 실패 자료에는 자원 PASS를 부여하지 않는다.
9. `has_pending_measurement_transactions`의 순차 fixture로 gate 획득 전후 경쟁을 재현하고 session·agent job·watchdog 미생성과 gate 반환을 검사한다.

### 10.3 다음 검증 순서

1. `python -m pytest -q`
2. `python tools\run_stability_fault_suite.py`
3. 추가 변경이 있거나 주간 검증 시 `python tools\run_windows_stability_soak.py --duration-minutes 45`
4. `python tools\analyze_windows_soak_summary.py <summary.json>`을 실행하고 exit 0, quality pass, issues/findings 0을 확인
5. 새 Windows ZIP 빌드와 `tools\verify_release_zip.py`
6. Python 없는 Windows PC에서 저권한, 한글 경로, 임시 폴더, EXE-only 실행
7. Edge/Chrome/Android Chrome에서 업로드, 세 측정 방식, 취소, 부분 실패
8. NVDA로 탭, 상태 live region, 진행률, 그래프 키보드 탐색
9. 실제 운영 장비 설정을 바꾸지 않는 읽기 전용 1Gbps 이하 현장 측정

---

## 11. 수정하면 안 되는 기존 동작

다음 항목은 별도 호환성 계획 없이 바꾸면 안 된다.

1. 기존 업로드, 다운로드, 삭제 URL과 form 필드
2. `upload_log.csv`, 세 측정 CSV의 헤더, UTF-8 BOM, 기존 행 의미
3. 상세 JSON 구조와 결과 URL
4. HTTP/TCP Excel의 시트 의미, KST 시간, 세션 ID 비노출
5. TCP 프로토콜 v2와 `v0.4.3` 이후 클라이언트 호환 범위
6. 서버 전체에서 동시 네트워크 측정 한 건 제한
7. 유효하지 않은 경로와 실행 가능/능동 콘텐츠 업로드 차단
8. 삭제 허용 IP 검사와 저장소 밖 경로의 다운로드/삭제 거절
9. 운영체제를 위해 최소 1GiB를 남기는 디스크 예약
10. 방화벽과 관리자 권한을 자동 변경하지 않는 정책
11. 운영 장비 설정을 바꾸지 않고 조회/임시 전송만 하는 측정 방식
12. 일부 기능 실패가 파일 업로드 웹 서버 전체를 불필요하게 중단하지 않는 동작

인증, TLS, incident 이력 같은 기능은 필요성이 크더라도 기존 클라이언트와 URL을 갑자기 깨지 않는 별도 버전으로 설계해야 한다.

---

## 12. 최종 권고사항

현재 `v0.5.1` 소스는 초기 상태보다 데이터 무결성과 실패 가시성이 크게 좋아졌고 전체 회귀 458건, fault suite 32건과 실제 45분 Windows 반복 시험에서도 기능 계약과 반복 프로세스 자원 추이가 통과했다. 남은 작업의 우선순위는 새 기능 개발이 아니다. 동일 commit의 onedir ZIP과 GitHub asset을 검증한 뒤, 실제 브라우저와 현장 환경에서 사용자 흐름을 확인하는 일이 먼저다.

### 즉시 수정해야 할 항목 5개

전체 회귀 458건과 fault suite 32건, 실제 2,708.89초 Windows soak의 검증 범위에서 즉시 재현된 잔여 P0는 없다. 다음은 배포 전 P1 검증 또는 사용성 보완이다.

1. 현재 `v0.5.1` clean commit으로 onedir EXE/ZIP을 만든 뒤 저권한·한글 경로·Python 미설치 검증
2. 실제 Edge/Chrome/Android/NVDA에서 첫 화면, 부분 완료, API 지연과 키보드 흐름 검증
3. 인증/TLS와 trusted-host가 없는 현재 서비스의 신뢰망 배치, 평문 token, Host 기반 client URL과 접근 통제 확인
4. 단일 서버 프로세스 장기 실행으로 heap/private bytes와 watcher 누수 검증
5. 일반 파일 업로드의 바이트 진행률을 기존 POST fallback과 함께 검증

### 사용성 개선 효과가 가장 큰 항목 5개

1. 완료·취소·실패와 성능 미판정을 구분한 관리자/초급 사용자 용어
2. 업로드·삭제·결과 저장의 안정 오류 코드와 다음 조치
3. 빠른 HTTP와 시간 기준 HTTP 전체 측정의 부분 결과 보존
4. 일반 파일 업로드의 실제 바이트 진행률 추가
5. 관리자 상세 접힘 위치와 키보드·NVDA 흐름의 실제 브라우저 조정

### 안정성 개선 효과가 가장 큰 항목 5개

1. 구현한 측정 JSON-CSV process-kill durable 복구와 충돌 fail-closed 유지
2. pending marker의 측정 시작 전 503 차단, TCP gate 경쟁 해제와 복구 오류 코드 보존 유지
3. 이미 구현한 업로드/삭제 트랜잭션, 백그라운드 failure counter와 health degraded 유지
4. 웹 요청, 파일 업로드, 측정, TCP 에이전트의 상한과 세션 ID 기반 취소 유지
5. 2,708.89초 soak의 quality gate와 `PASS_NO_REPEATED_PROCESS_GROWTH` 회귀를 유지하고 단일 프로세스 장기 시험을 별도 수행

### 가장 먼저 수정할 파일 또는 모듈

다음 배포 작업의 첫 대상은 `tools/build_windows_release.ps1`, `tools/verify_release_zip.py`와 `.github/workflows/release.yml`이다. 전체 회귀 458건, fault 32건, compileall, 5개 JavaScript 구문 검사, diff-check와 45분 soak 분석이 이미 통과했으므로 서버 업무 코드를 더 바꾸기보다 현재 `v0.5.1` 상태 그대로 새 onedir ZIP을 만들어야 한다. verifier로 실행 파일 구성, 보안 산출물, SHA256, 기본 `config.ini`, header-only CSV와 운영 결과 미포함을 확인한 뒤 같은 commit의 원격 annotated tag에서만 게시한다.

### 첫 번째 작업 단위에서 수행할 구체적인 변경사항

1. 기존 JSON과 CSV 형식을 바꾸지 않는 `measurement_transactions.py`와 source별 semantic recovery를 적용했다.
2. marker에 예상 행 전체, JSON semantic hash와 rollback 상태를 원자 저장하고 marker→JSON→행별 `fsync` CSV→marker 정리 순서를 적용했다.
3. pending marker가 있으면 HTTP/TCP 측정을 시작 전에 503으로 차단하며, TCP gate 직후 경쟁에서도 gate를 반환하고 session·agent job·watchdog을 만들지 않는다.
4. 저장 직전 생긴 `MEASUREMENT_RECOVERY_PENDING`을 일반 `RESULT_WRITE_FAILED`로 덮지 않고 사용자가 재시작 필요성을 알 수 있게 보존했다.
5. 45분 soak analyzer에 duration·기능·구조·tail 품질 계약, 네 anomaly와 exit 0/1/2/3 계약을 적용했고 실제 실행에서 quality pass, issues/findings 0, `PASS_NO_REPEATED_PROCESS_GROWTH`를 확인했다.

### 완료된 첫 안정성 작업 단위

업로드·삭제 경계에는 이미 `upload_transactions.py`를 적용했다. prepared marker, 파일/CSV 단계 기록, terminal marker cleanup, 경로 충돌 fail-closed, 파일 확정 직후 및 삭제 CSV 갱신 직후 kill test가 현재 작업트리와 자동 테스트에 포함돼 있다. 측정 저장 복구, pending start 차단, 결과 JSON 단일 읽기와 안전한 실패 처리, 2,708.89초 Windows soak와 전체 회귀 458건도 완료됐다.

### 다음 작업 단위

1. 결정된 `v0.5.1` 버전으로 clean onedir ZIP, 보안 산출물과 SHA256을 검증하고 같은 commit의 원격 annotated tag를 게시한다.
2. 하나의 서버 프로세스를 장시간 유지하는 별도 시험으로 heap/private bytes와 watcher 누수를 확인한다.
3. Computer Use URL 안전 확인 3회 실패로 남은 실제 Edge/Chrome/Android/NVDA 검증을 민감정보 없는 수동 체크리스트로 수행한다.
4. 실제 브라우저 체크리스트에서 확인된 문제만 작은 독립 변경으로 수정한다.
5. 일반 파일 업로드 진행률은 기존 form POST fallback과 URL을 유지한 XHR 향상으로 별도 적용한다.

새 관리자 기능, 인증, incident 이력, 대규모 리팩터링은 이 검증이 끝난 뒤 별도 범위로 다룬다.
