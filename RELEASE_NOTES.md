# Release Notes 운영 규칙

현재 앱 버전: `v0.5.2`

이 파일은 저장소에 커밋하는 릴리즈 준비 점검 문서입니다. 현재 GitHub Actions가 Windows 실행 ZIP을 만들고 GitHub Release에 업로드합니다. Release 본문은 이 파일과 `CHANGELOG.md` 기준으로 작성합니다.

## v0.5.2 - 2026-08-05

`v0.5.1`은 원격 annotated tag와 checkout commit 검증을 통과했지만, Windows CI에서 TCP timeout 세션의 결과 영속화가 끝나기 전에 measurement gate를 확인한 테스트 3건이 경합으로 실패했습니다. GitHub Release와 asset은 생성되지 않았고 기존 태그는 이동하거나 삭제하지 않았습니다.

`v0.5.2`는 timeout 회귀 테스트가 `persistence_complete`까지 제한 시간 안에 기다린 뒤 gate 해제를 확인하도록 안정화합니다. TCP 결과 저장 뒤에는 gate를 먼저 해제하고 같은 임계구역에서 완료 플래그를 공개해, 완료로 보인 직후 새 측정이 일시적으로 거절되는 창도 제거합니다. 결과 저장이 끝나기 전에는 새 측정을 막는 기존 안전 계약, 기존 URL·CSV·JSON·Excel 형식과 TCP 프로토콜 `v2` 호환성은 변경하지 않습니다.

게시 전 게이트:

- 전체 회귀 458건, 장애 주입 32건, Python compileall, JavaScript 5개 구문 검사와 `pip check`
- clean Windows onedir 빌드, 서버 smoke·TCP 자체 점검, 클라이언트 자체 점검과 한글 경로 실행
- ZIP verifier, `security_manifest.source_commit`, SHA256와 실행 CMD UTF-8 no-BOM 검사
- 같은 commit을 가리키는 원격 annotated tag와 `gh release create --verify-tag`

## v0.5.1 - 2026-08-05

`v0.5.0` annotated tag push는 Actions checkout이 로컬 tag ref를 commit object로 평탄화해 immutable-tag 타입 검사에서 빌드 전에 중단됐습니다. GitHub Release와 asset은 생성되지 않았고 기존 태그는 이동하거나 삭제하지 않았습니다.

`v0.5.1`은 원격의 정확한 tag ref를 force-fetch한 뒤 annotated object type과 checkout commit을 검사합니다. 이 검사는 통과했지만 Windows CI의 비동기 timeout 테스트 3건이 결과 영속화 완료 전 gate를 확인해 실패했고 GitHub Release와 asset은 생성되지 않았습니다. `v0.5.0`에 준비한 모든 사용성·안정성 개선, 기존 URL·CSV·JSON·Excel 형식과 TCP 프로토콜 `v2` 호환성을 그대로 포함합니다.

게시 전 게이트:

- 전체 회귀 458건, 장애 주입 32건, Python compileall, JavaScript 5개 구문 검사와 `pip check`
- clean Windows onedir 빌드, 서버 smoke·TCP 자체 점검, 클라이언트 자체 점검과 한글 경로 실행
- ZIP verifier, `security_manifest.source_commit`, SHA256와 실행 CMD UTF-8 no-BOM 검사
- 같은 commit을 가리키는 원격 annotated tag와 `gh release create --verify-tag`

## v0.5.0 - 2026-08-05

기존 업로드·다운로드 URL, CSV·JSON·Excel 형식, TCP 프로토콜 `v2`와 핵심 작업 순서를 유지하면서 데이터 무결성, 실패 가시성, 초급 사용자 안내와 관리자 상태 요약을 보강한 정식 릴리즈입니다.

주요 변경:

- 업로드·삭제의 파일/CSV 경계와 HTTP/TCP 측정의 JSON/CSV 경계에 durable transaction과 재시작 복구 적용
- 손상·충돌 transaction은 자동 정상 추정하지 않고 fail-closed하며, 미정리 측정 marker는 새 세션과 네트워크 전송 전에 `MEASUREMENT_RECOVERY_PENDING` 503으로 차단
- 연결·명령·요청 시간 상한, 동시 실행 제한, 중복 실행 방지, 취소·종료 시 gate·소켓·요청 worker 정리 강화
- 0바이트, 잘못된 합계, 빈 결과와 변경된 응답 형식이 성공으로 저장되지 않도록 HTTP/TCP 결과 검증 강화
- 결과 JSON을 한 번만 읽고 삭제 경합·잘못된 UTF-8·손상 JSON을 경로·traceback 없는 `RESULT_READ_FAILED`로 반환
- 시작 설정, 포트 변경 거절, 저장 실패와 권한 오류를 안정된 한국어 오류 코드와 다음 조치로 안내
- 첫 화면의 작업 순서, 진행 상태, 중복 클릭 방지, 부분 실패 표현과 정상·주의·장애 구분 개선
- `/api/health`와 민감정보 없는 `/api/operations-summary`를 이용한 관리자 요약, 문제 우선 정렬과 권장 조치 추가
- 측정 API의 `no-store`·`nosniff`, 회전 진단 로그, 실패 counter와 기본 config/header-only CSV 저장소 검사 추가
- Windows 반복 시험에 working set·handle·thread·TCP socket 계측과 독립 분석기를 추가하고 주간 45분 workflow에 판정 gate 적용
- 실제 소스 근거, 사용자별 평가와 P0/P1/P2 계획을 담은 한국어 진단 보고서 추가
- Windows PowerShell 5.1용 스크립트 BOM, 실행 CMD의 UTF-8 no-BOM과 local/Actions native 종료 코드 검사를 적용하고, 기존 annotated tag와 checkout commit 일치·`--verify-tag`를 Release 게시 조건으로 강제

검증 근거:

- 전체 회귀 458건과 장애 주입 32건 통과
- Python `compileall`, JavaScript 5개 `node --check`, `pip check`, `git diff --check` 통과
- Windows 2,708.89초 반복 시험: 386 cycles, 업로드 101,187,584 bytes, TCP 자체 점검 386회, 772 processes/1,544 samples
- soak 분석 결과: quality `pass`, issues 0, findings 0, `PASS_NO_REPEATED_PROCESS_GROWTH`
- 로컬 `v0.5.0` onedir ZIP의 서버 smoke, 서버 TCP 자체 점검, 클라이언트 자체 점검, 보안 산출물과 ZIP verifier는 통과했습니다. `v0.5.0`과 `v0.5.1` tag workflow는 각각 immutable-tag 검사와 비동기 테스트 경합으로 중단돼 Release asset을 게시하지 않았으며 `v0.5.2`가 이를 대체합니다.

남은 운영 한계:

- 코드서명, 로그인과 TLS는 포함하지 않습니다. 신뢰 VLAN과 ACL 밖에 노출하지 마세요.
- HTTP/TCP 토큰과 데이터는 평문이며 요청 Host 기반 클라이언트 주소 생성, 파일 크기 무제한, 압축 내부 미검사와 장기 폴링이 유지됩니다.
- 45분 PASS는 반복 재기동 자원 추이에 관한 판정이며 단일 프로세스 장기 누수 부재를 증명하지 않습니다.
- 실제 Edge/Chrome/Android/NVDA, 한글·저권한 현장 경로와 실제 사내 네트워크 품질은 배포 환경에서 별도 확인해야 합니다.

## v0.4.6 - 2026-07-16

`v0.4.6-rc.2`의 Windows 전체 시험 262건, 서버·TCP 클라이언트 자체 점검과 배포 ZIP 검증을 통과한 내용을 정식 릴리즈로 승격합니다. 기능과 데이터 형식은 `rc.2`에서 변경하지 않았습니다.

## v0.4.6-rc.2 - 2026-07-16

기존 기능과 데이터 형식을 유지하면서 동시 업로드, 서버 종료와 Windows 전원 중단 상황의 안정성을 보강한 사전 릴리즈입니다.

- 파일 업로드를 최대 4건으로 제한하고 진행 중인 모든 요청의 남은 예상 용량을 합산해 디스크 공간 예약
- 업로드 동시 실행 한도는 HTTP 503, 합산 용량 부족은 HTTP 507로 구분하고 `/api/health`에 현재 점유 상태 표시
- Ctrl+C 종료 시 신규 웹 요청을 차단하고 진행 중 요청을 최대 30초 기다린 뒤 TCP·웹 서버 종료
- Windows의 업로드 파일, CSV, JSON과 설정 파일 최종 교체에 `MoveFileExW`의 `REPLACE_EXISTING | WRITE_THROUGH` 적용
- 매주 45분, 수동 30·45·60분 Windows 업로드·TCP·강제 재시작 반복 시험 추가
- Windows 반복 시험이 하위 서버와 TCP 자체 점검을 실행할 때 UTF-8 출력을 강제해 비영문 시스템 로캘과 무관하게 실행

업로드 파일과 CSV·JSON·Excel 형식, 네트워크 측정 계산식, TCP 프로토콜 `v2`는 변경하지 않습니다.

## v0.4.6-rc.1 - 2026-07-16

첫 Windows 검증에서 반복 시험 하위 프로세스가 GitHub 러너의 CP1252 출력 인코딩을 상속해 한글 시작 문구에서 종료되는 문제를 발견했습니다. Windows 실행 ZIP과 GitHub Release는 게시하지 않았으며 실패 이력 보존을 위해 태그만 유지합니다.

## v0.4.5-rc.2 - 2026-07-16

파일 저장, 운영 기록과 장기 실행 중 장애 복구를 보강한 안정성 사전 릴리즈입니다.

- 네 종류 운영 CSV의 시작 검증과 마지막 미완성 레코드 백업 복구
- 헤더·중간 레코드 손상 시 데이터 보존을 위한 시작 중단
- 다운로드·삭제 대상이 현재 `STORAGE_ROOT` 내부인지 재검증
- HTTP 용량 측정 15분, HTTP 시간·TCP 측정 5분의 잠금 유지 상한
- 같은 `data` 폴더를 사용하는 서버의 운영체제 수준 중복 실행 차단
- `data/diagnostics`의 2MB 순환 로그와 저장소·CSV·TCP·측정 상태를 포함한 `/api/health`
- 업로드 전 예상 크기와 업로드 중 8MB 간격 디스크 확인, 1GB 운영 여유 공간과 HTTP 507 처리
- 웹 요청 스레드 최대 32개, 30초 무동작 연결 종료와 한도 초과 HTTP 503 처리
- 업로드 CSV 메모리 색인, 측정 CSV 10,000행 기준 월별 보관과 상세 JSON 유형별 최신 1,000건 유지
- CSV별 복구 백업 최신 5개 유지와 종료된 프로세스의 업로드 임시 파일 즉시 정리
- Windows의 Win32 프로세스 상태 API를 사용한 강제 종료 직후 업로드 예약 정리
- `/api/health`의 디스크·CSV 검사 5초 캐시와 로컬 장애 재현 시험 실행기

기존 업로드 파일과 CSV·JSON·Excel 형식, HTTP·TCP 계산식, TCP 프로토콜 `v2`는 유지합니다. 오래된 측정 상세 JSON의 보관 개수만 제한하며 CSV 요약은 활성 파일 또는 월별 파일로 보존합니다.

## v0.4.5-rc.1 - 2026-07-16

Windows 장애 시험에서 종료된 업로드 프로세스를 실행 중으로 잘못 판정하는 문제를 발견해 소스 검사 단계에서 빌드를 중단했습니다. Windows 실행 ZIP과 GitHub Release는 게시하지 않았으며 실패 이력 보존을 위해 태그만 유지합니다.

## v0.4.4-rc.1 - 2026-07-16

사내 관제에서 악성 의심 파일로 분류될 가능성을 낮추고 실제 동작을 검토 가능하게 만든 보안 구조 개선 사전 릴리즈입니다. 코드서명이나 사내 허용목록 지원이 없는 조건이므로 탐지 해제를 보장하지는 않습니다.

실행 구조:

- 실행 때 임시 폴더에 자체 압축 해제하는 PyInstaller `onefile` 대신 `_internal` 폴더를 사용하는 포터블 `onedir` 배포
- 서버 전용 `InternalUploadServer.exe`와 TCP 측정 전용 `NetworkProbeClient.exe`를 별도 진입점으로 분리
- 클라이언트 ZIP에서 서버 EXE와 CMD를 제거하고 `client-config.json`, 파일 해시가 담긴 `client-manifest.json`과 안내문만 추가
- 서버 시작 중 PowerShell·`ExecutionPolicy Bypass`와 자동 방화벽 조회를 제거하고 실제 포트 점검과 수동 확인 안내만 유지
- 실행파일에 제품명, 버전, 역할과 원본 파일명을 나타내는 Windows 버전 정보 추가

파일 취급:

- 실행파일, 스크립트, 바로가기, 설치 패키지, 드라이버, 매크로 문서와 디스크 이미지 업로드 차단
- 확장자를 바꾼 Windows PE 파일은 `MZ` 헤더로 추가 차단
- 다운로드는 `application/octet-stream`, 첨부파일, `Cache-Control: no-store`, `X-Content-Type-Options: nosniff` 적용
- 일반 문서, PCAP, EVTX와 압축파일은 허용하며 압축파일 내부 검사와 파일 크기 제한은 추가하지 않음

공급망과 검토 자료:

- Windows 빌드 의존성을 버전과 SHA256 해시가 고정된 `requirements-windows.lock`으로 설치
- GitHub Actions를 커밋 SHA로 고정하고 이미 존재하는 태그를 덮어쓰지 않는 불변 릴리즈 방식 적용
- 배포 ZIP에 `SECURITY_REVIEW_KO.md`, `security_manifest.json`, CycloneDX `sbom.cdx.json`, `SHA256SUMS.txt` 포함
- 서버·클라이언트 자체 점검, 패키지 역할 분리와 보안 산출물 해시를 릴리즈 검증기에 추가

남는 운영 위험:

- 코드서명이 없어 SmartScreen 또는 보안 제품 탐지가 계속될 수 있음
- 사내망 전체 무인증 접근, 파일 크기 무제한과 압축파일 내부 미검사는 유지
- TCP 클라이언트는 웹에서 작업을 받을 때까지 설정된 서버 한 곳으로 장기 폴링
- TCP 프로토콜 `v2`, 측정 계산식, HTTP 측정, CSV·JSON·Excel 형식과 기존 저장 위치는 변경하지 않음

## v0.4.3-rc.2 - 2026-07-15

HTTP·TCP 측정 결과의 그래프와 Excel 보고서를 초급 네트워크 엔지니어와 관리자 모두 쉽게 읽도록 정리한 사전 릴리즈입니다.

화면 개선:

- HTTP 시간 측정과 TCP 측정의 업로드·다운로드 그래프를 각각 분리
- 모든 그래프를 0Mbps 기준 축, 읽기 좋은 자동 상한, 평균선, 최저·최고 표시로 통일
- HTTP 다운로드 구간값에 브라우저 표본의 실제 경과시간을 반영해 전체 평균과 구간 통계의 기준을 일치
- 마우스·터치·키보드로 각 초의 Mbps와 MB/s 확인
- TCP 완료 그래프를 접힌 영역에서 꺼내 결과 직후 표시
- 500ms 상태 조회 사이의 TCP 진행률을 브라우저 애니메이션으로 보간하고 완료 시에만 100% 확정

Excel 개선:

- 서버 PC 시간대와 무관하게 모든 Excel 시각을 한국 표준시(KST)로 변환
- 내부 세션 ID를 Excel 시트와 파일명에서 제거하고 JSON·CSV·API 내부 추적에는 유지
- HTTP Excel을 `결과 요약`, `속도 변화` 두 시트로 단순화
- TCP Excel을 `결과 요약`, `속도 변화`, `기술 상세`로 정리하고 원시 TCP 통계 보존
- 요약은 평균 Mbps, MB/s, 최저·최고와 HTTP 변동·응답시간 또는 TCP RTT·재전송률만 표시
- Excel 그래프에 평균선과 숫자 라벨을 추가하고 30초 결과는 5초 간격과 최저·최고만 표시

호환성과 배포:

- 전체 평균 처리량 계산, TCP 측정 프로토콜·전송 방식, API, CSV·JSON 필드와 저장 위치는 변경하지 않음
- TCP 프로토콜은 `v2`를 유지하므로 `v0.4.3-rc.1` 클라이언트와 호환
- 기존 저장 JSON에서도 새 Excel 형식으로 다운로드 가능
- Windows 실행 asset은 `internal-upload_v0.4.3-rc.2_windows.zip`과 동일 이름의 `.sha256`
- `v0.4.3-rc.1`과 이전 릴리즈 파일은 삭제하거나 덮어쓰지 않음
- UDP와 Android 네이티브 TCP 클라이언트는 이번 범위에 추가하지 않음

## v0.4.3-rc.1 - 2026-07-15

TCP 클라이언트가 웹 API에만 등록되고 실제 TCP 측정 포트에는 연결하지 못하는 상태를 측정 전에 구분하기 위한 안정성 검증 사전 릴리즈입니다.

연결 안정성:

- 클라이언트 등록 직후와 대기 중 약 20초마다 실제 TCP 측정 포트를 3초 제한으로 자동 점검
- 최근 45초 이내 점검이 성공한 `준비 완료` 클라이언트만 측정 시작 허용
- 시간 초과, 연결 거부, PC 이름 해석 실패, 경로 없음, 프로토콜 오류를 구분해 웹 화면에 한국어로 표시
- 실패 시 서버 콘솔 TCP 포트와 Windows 방화벽 인바운드 허용 확인 안내
- 자동 점검은 작은 제어 메시지만 주고받으며 속도 측정 부하를 발생시키지 않음

버전 및 호환성:

- 서버·클라이언트 릴리즈 버전과 TCP 프로토콜 버전을 상태 API와 화면에 표시
- 같은 TCP 프로토콜의 릴리즈 불일치는 경고만 표시하고 측정 허용
- TCP 프로토콜을 `v2`로 올려 `v0.4.2` 및 이전 클라이언트는 등록을 거부하고 최신 Windows 클라이언트 ZIP 재다운로드 안내
- 소스의 `APP_VERSION`과 Windows 빌드 인자가 다르면 릴리즈 빌드 중단

표현 및 변경 범위:

- 현행 화면·콘솔·클라이언트 안내의 `TCP 정밀 측정`을 `TCP 전송 성능 측정`으로 통일
- HTTP 안내의 `시간당 전송 성능`을 `시간 기준 전송 성능`으로 바꿈
- TCP 데이터 엔진·계산식·결과 CSV·JSON·Excel 스키마는 변경하지 않음
- HTTP 측정 방식과 파일 업로드·다운로드 기능은 변경하지 않음
- UDP, Android 네이티브 클라이언트와 방화벽 자동 변경은 추가하지 않음

사내 검증:

- 원본 속도 결과를 반출하지 않고도 `준비 완료`, 업로드·다운로드 완료, 실패 안내 여부만으로 사전 릴리즈 수락 가능
- 검증 후 문제가 없을 때만 후속 정식 릴리즈 여부를 다시 검토

## v0.4.2 - 2026-07-15

HTTP·TCP 네트워크 측정 결과를 초급 네트워크 엔지니어와 관리자가 빠르게 읽을 수 있도록 정보 계층을 정리한 화면 개선 릴리즈입니다.

화면 개선:

- HTTP 시간 기준 측정 중에는 현재 단계, 남은 시간, 전체 진행률과 최근 3초 평균 속도 하나만 표시
- HTTP 완료 화면은 업로드·다운로드 평균 속도와 변동률을 우선 표시하고 그래프는 완료 후에만 표시
- HTTP 응답시간, 중앙값, 최저·최고, 전송량과 측정 조건은 `기술 상세 보기`로 이동
- TCP 일반 측정은 1개 스트림을 기본으로 사용하고 4개 스트림은 접힌 `고급 비교 측정`에서만 선택
- TCP 완료 화면은 실제 수신 평균 속도만 우선 표시하고 그래프와 기술 통계는 각각 접어서 제공
- Mbps를 대표 속도로, MB/s를 `초당 파일 전송량`으로 설명하고 업로드·다운로드 경로를 함께 표시

호환성과 제한:

- HTTP·TCP 측정 엔진, 계산식, API, CSV·JSON 필드와 저장 경로는 변경하지 않습니다.
- 기존 Excel의 요약·구간·스트림 상세 데이터와 정밀도는 유지합니다.
- 기준 속도가 없어 정상·비정상을 자동 판정하지 않으며 단말과 서버 성능이 결과에 포함됩니다.
- UDP 정밀 측정과 Android 네이티브 TCP 클라이언트는 추가하지 않았습니다.
- `v0.4.1`과 기존 릴리즈 파일을 보존합니다.

## v0.4.1 - 2026-07-15

`v0.4.0`의 기능과 형식을 유지하면서 장기 실행, 동시 접속과 전원·프로세스 중단 상황의 내구성을 높인 유지보수 릴리즈입니다.

안정성 개선:

- 업로드 내용을 전용 `.part` 파일에 완전히 저장하고 `fsync` 후 원자적으로 최종 이름 확정
- 시작 시 24시간이 지난 프로젝트 전용 업로드 임시·예약 파일 정리
- TCP 완료 세션 메모리 보존을 30분·최근 100건으로 제한하고 종료 소켓 참조 즉시 제거
- TCP 측정 포트 초기 핸드셰이크 동시 처리를 16개로 제한하고 종료 시 잔류 핸드러 정리
- 업로드·HTTP 측정 CSV 읽기와 쓰기에 동일한 잠금 적용
- HTTP·TCP JSON을 임시 파일 `fsync` 후 원자적으로 저장하고 TCP 결과 저장 완료 후에만 JSON·Excel 링크 공개

호환성과 제한:

- 설정 파일, CSV·JSON 저장 경로와 기존 필드를 유지합니다.
- TCP 상태 API에 결과 저장 상태를 위한 추가 필드만 넣었습니다.
- UDP 정밀 측정과 Android 네이티브 TCP 클라이언트는 추가하지 않았습니다.
- `v0.4.0`과 기존 사전 릴리즈 파일을 보존합니다.

## v0.4.0 - 2026-07-15

`v0.4.0-rc.1`부터 `v0.4.0-rc.8`까지 검증한 HTTP·TCP 네트워크 측정 기능을 정식으로 배포하는 릴리즈입니다.

포함 내용:

- 데이터량 또는 측정 시간을 선택하는 브라우저 기반 HTTP 전송 측정
- Windows 서버와 클라이언트 사이의 자체 TCP 전송 성능 측정
- 현재 서버 주소를 자동 포함하는 Windows 클라이언트 ZIP 다운로드
- 업로드·다운로드 실제 수신 속도, RTT와 재전송 비율 중심의 웹 결과
- 관리자용 요약과 구간별 속도, 스트림 기술 상세를 제공하는 Excel 결과
- 포트 충돌 감지와 사용자 승인 기반 자동 포트 변경
- 기존 파일 업로드, 메모, 저장 폴더, 직접 다운로드 링크와 CSV 기록

호환성과 제한:

- `v0.4.0-rc.8`과 측정 엔진, API, JSON·CSV 형식 및 저장 경로가 같습니다.
- UDP 정밀 측정과 Android 네이티브 TCP 클라이언트는 포함하지 않습니다.
- 기준 속도를 설정하지 않으므로 TCP 결과를 정상·비정상으로 자동 판정하지 않습니다.
- 기존 사전 릴리즈와 배포 파일은 삭제하지 않습니다.

## v0.4.0-rc.8 - 2026-07-15

TCP 측정 결과를 네트워크 초급 관리자와 보고 대상자가 빠르게 읽을 수 있도록 표현을 정리한 사전 릴리즈입니다.

추가·개선된 내용:

- 사용자 화면의 `TCP 정밀 측정` 명칭을 `TCP 전송 성능 측정`으로 변경
- 업로드는 `측정 PC → 서버`, 다운로드는 `서버 → 측정 PC` 경로를 함께 표시
- 방향별 결과를 실제 수신 평균, MB/s, 중앙값, 최소와 최대 속도로 정리
- 웹 요약을 업로드·다운로드 실제 수신 속도, RTT, TCP 재전송, 측정 조건과 측정 PC의 여섯 항목으로 단순화
- 재전송량과 전체 송신량 대비 비율을 함께 표시하고 CWND는 Excel 기술 상세로 이동
- 전체 측정에는 업로드와 다운로드 실제 수신 평균의 차이 비율만 객관적으로 표시
- Excel 첫 시트를 실제 수신 속도와 핵심 TCP 상태 중심의 관리자 요약으로 재구성
- Excel 구간 시트에서 송신 측 기록과 수신 측 실제 값을 명확하게 구분하고 기존 스트림 기술 상세는 보존
- 측정하지 않은 방향과 운영체제 미지원 통계를 서로 다른 문구로 표시

호환성과 제한:

- 기준 속도를 설정하지 않으므로 정상·비정상을 자동 판정하지 않습니다.
- TCP 측정 엔진, API, JSON·CSV 필드와 저장 경로는 변경하지 않습니다.
- UDP 정밀 측정은 포함하지 않습니다.
- 기존 `v0.4.0-rc.7` 릴리즈와 파일은 보존합니다.

## v0.4.0-rc.7 - 2026-07-14

HTTP 측정 선택을 단순화하고 TCP 정밀 측정 결과를 현장에서 바로 검토할 수 있도록 개선한 사전 릴리즈입니다.

추가·개선된 내용:

- `HTTP 용량 기준`과 `HTTP 시간 기준`을 하나의 `HTTP 전송 측정`으로 통합
- HTTP 안에서 종료 기준을 `데이터량` 또는 `측정 시간`으로 선택
- 반복 실패하던 HTTP 시간 기준 4개 연결을 제거하고 UI와 서버를 1개 연결로 고정
- 데이터량 기준 완료 결과에 전송량, 걸린 시간, 최종 평균, 초당 MB와 1GB 예상 시간 표시
- Mbps와 MB/s의 차이 및 예상 시간의 전제 조건을 화면에 안내
- TCP 결과 화면의 JSON 버튼을 Excel 버튼으로 교체하고 `측정 요약`, `구간별 속도`, `스트림 상세` 제공
- 성공·실패·취소 TCP 결과를 저장된 JSON에서 요청 시 메모리로 Excel 생성
- 기존 HTTP·TCP CSV와 JSON 저장 및 JSON API 유지

호환성과 제한:

- 이전에 저장된 HTTP 4개 연결 JSON과 Excel 결과는 계속 읽을 수 있습니다.
- TCP Excel에서 Windows TCP_INFO 미지원 값은 추정하지 않고 `값 없음`으로 표시합니다.
- UDP 정밀 측정은 포함하지 않습니다.
- 기존 `v0.4.0-rc.6` 릴리즈와 파일은 보존합니다.

## v0.4.0-rc.6 - 2026-07-14

HTTP 측정 방식의 이름을 종료 기준에 맞게 정리하고 TCP 정밀 측정을 별도 설정 편집 없이 사용할 수 있게 개선한 사전 릴리즈입니다.

추가·개선된 내용:

- `간편 측정`과 `지속 측정`을 `HTTP 용량 기준`과 `HTTP 시간 기준`으로 변경
- 화면, 진행 문구, 오류, Excel 제목과 현행 사용 문서의 용어 통일
- 신규 설치와 기존 설정 업데이트에서 TCP 정밀 측정을 기본 활성화
- 일회성 `CONFIG_VERSION=2` 마이그레이션 후에는 사용자가 다시 `ENABLED=false`로 설정한 선택을 유지
- TCP `5201` 포트 충돌 시 다음 99개 포트에서 빈 포트를 제안하고, 실제 바인딩 성공 후에만 설정 저장
- TCP 포트 변경 거절·후보 고갈·바인딩 실패 시에도 파일 업로드 웹 서버는 계속 실행
- TCP 탭 상단에 현재 서버 주소가 자동 포함된 `Windows 클라이언트 ZIP 받기` 주 버튼 제공
- TCP 데이터 포트는 등록 후 서버가 자동 전달하므로 포트만 바뀐 경우 클라이언트 ZIP 재다운로드 불필요

호환성과 제한:

- HTTP·TCP 측정 알고리즘, API, CSV·JSON 형식과 저장 경로는 변경하지 않습니다.
- Windows 방화벽 규칙과 관리자 권한은 자동으로 변경하지 않고 현재 TCP 포트의 수동 허용 명령을 표시합니다.
- 브라우저 보안 제한으로 ZIP 압축 해제와 CMD 실행은 사용자가 직접 수행합니다.
- 기존 `v0.4.0-rc.5` 릴리즈와 파일은 보존합니다.

## v0.4.0-rc.5 - 2026-07-14

HTTP 지속 측정의 진행바가 단계마다 되돌아가거나 계단식으로 움직이던 표시 문제를 개선한 사전 릴리즈입니다.

추가·개선된 내용:

- 응답시간 확인에 5%, 전체 워밍업·본 측정에 실제 시간 비율로 95%를 배정
- 업로드·다운로드 전체 측정에서도 진행률이 0%에서 100%까지 한 번만 증가
- 전송 요청과 상태 API 응답 주기에서 진행바를 분리하고 브라우저 시간 기준으로 부드럽게 갱신
- 현재 단계 문구에 전체 퍼센트와 단계 경과 시간을 함께 표시
- 진행 중에는 최대 99.9%로 유지하고 결과 저장까지 성공한 경우에만 100% 확정
- 실패·취소 시 실제 중단 위치의 진행률 유지

호환성과 제한:

- HTTP 전송, 속도 계산, 1초 그래프, CSV·JSON·Excel 결과 형식과 서버 API는 변경하지 않습니다.
- 자동 테스트, 자체 점검과 Windows CI로 검증합니다.
- 기존 `v0.4.0-rc.4` 릴리즈와 파일은 보존합니다.

## v0.4.0-rc.4 - 2026-07-14

HTTP 지속 측정 결과를 현장에서 바로 비교하고 전달할 수 있도록 Excel 보고서를 추가한 사전 릴리즈입니다.

추가·개선된 내용:

- 지속 측정 결과 화면의 `상세 JSON 받기`를 `Excel 결과 받기`로 교체
- `측정 요약`, `구간별 속도` 두 시트에 상태·오류·조건·응답시간·방향별 속도와 1초 그래프 제공
- Mbps와 MB/s, Byte와 MiB를 함께 기록해 전송량과 처리량을 구분
- 성공뿐 아니라 실패·취소 결과도 수집된 구간까지 Excel로 내보내기
- 기존 세션별 JSON 저장과 JSON API는 호환성과 원본 보존을 위해 유지
- Excel은 저장된 JSON을 기준으로 요청 시 메모리에서 생성하며 서버 디스크에 중복 저장하지 않음
- 기존 결과 IP 제한, `Cache-Control: no-store`, Excel 수식 삽입 방지 적용

운영 방식과 제한:

- 보고서는 웹 HTTP 지속 측정 결과이며 TCP 정밀 측정 또는 UDP 결과를 포함하지 않습니다.
- 측정 위치, AP 이름, 판정 기준과 메모 입력은 이번 범위에 추가하지 않습니다.
- 기존 `v0.4.0-rc.3` 릴리즈와 파일은 보존합니다.

## v0.4.0-rc.3 - 2026-07-14

웹 포트 충돌 시 설정 파일을 직접 편집하지 않아도 되도록 시작 절차를 개선한 사전 릴리즈입니다.

추가·개선된 내용:

- 설정 웹 포트가 사용 중이면 다음 99개 포트를 순서대로 검사해 가장 가까운 빈 포트 제안
- Enter 또는 `Y` 승인 후 실제 서버 바인딩에 성공한 경우에만 `config.ini`의 `PORT` 저장
- `N`, 입력 불가, 후보 포트 고갈과 최종 바인딩 실패 시 기존 설정 보존
- 동일한 InternalUpload 서버의 `/api/health` 응답을 확인해 중복 실행 대신 기존 주소 안내
- `BASE_URL`이 기존 웹 포트를 사용할 때만 새 포트로 동기화하고 별도 포트는 유지
- Windows 방화벽 인바운드 허용 상태 확인과 관리자용 수동 명령 안내
- 실제 실행 포트를 콘솔에 표시하고 새 포트에서 받은 TCP 클라이언트 ZIP에 해당 포트를 자동 포함

운영 방식과 제한:

- 방화벽 규칙과 관리자 권한은 자동으로 변경하지 않습니다.
- TCP 정밀 측정 포트 `5201`은 웹 포트 충돌 처리 대상이 아닙니다.
- 포트가 바뀌기 전에 받은 자동 연결 클라이언트 ZIP은 웹 화면에서 다시 받아야 합니다.
- 기존 `v0.4.0-rc.2` 릴리즈와 파일은 보존합니다.

## v0.4.0-rc.2 - 2026-07-10

TCP 측정 클라이언트 배포를 단순화한 사전 릴리즈입니다.

추가·개선된 내용:

- TCP 정밀 측정 화면에서 `Windows 클라이언트 ZIP 받기` 제공
- 브라우저가 접속한 서버 PC 이름 또는 IPv4와 웹 포트를 자동 포함
- `localhost`와 `127.0.0.1` 접속 시 감지된 사내 IPv4와 설정 웹 포트로 대체
- 압축 해제 후 주소 입력 없이 실행되는 자동 연결 `start_tcp_probe_client.cmd`
- 클라이언트 ZIP을 EXE, CMD, 한국어 안내문 세 파일로 제한하고 설정·토큰·세션 정보 제외
- Host 값 검증과 배치 명령 삽입 방지, 소스 실행·비활성 서버·EXE 누락 시 다운로드 차단
- `--probe-self-check`에서 실행 중인 Windows EXE의 클라이언트 ZIP 생성과 내부 구조 검증
- SHA256 파일을 LF 줄바꿈으로 생성해 Windows, macOS와 Linux 검증 명령에서 공통 사용

운영 방식과 제한:

- 서버 IP 또는 웹 포트가 바뀌면 클라이언트 ZIP을 다시 받습니다.
- 전체 Release ZIP의 주소 입력형 `start_tcp_probe_client.cmd`는 수동 대체 수단으로 유지합니다.
- 같은 스위치의 Windows 11 PC 두 대에서 iperf 3회 중앙값 대비 ±10%를 확인한 뒤 정식 `v0.4.0`을 게시합니다.
- UDP와 Android 네이티브 TCP 클라이언트는 포함하지 않습니다.

## v0.4.0-rc.1 - 2026-07-10

Windows 자체 TCP 정밀 측정을 추가한 사전 릴리즈입니다.

추가된 내용:

- 동일한 `InternalUpload.exe`의 TCP 서버 및 `--probe-client` 대기형 클라이언트 모드
- 클라이언트 주소 입력용 `start_tcp_probe_client.cmd`
- TCP 업로드·다운로드·전체, 1개·4개 스트림, 3초 워밍업, 10초·30초 측정
- 수신·송신 처리량, 1초 그래프, RTT, 최소 RTT, CWND, 재전송 바이트
- 자동 임시 토큰과 접속 IP 결합, 취소·연결 중단·만료 처리
- HTTP 간편·지속·TCP 측정의 서버 전체 동시 실행 1건 제한
- `data/network_probe_log.csv` 요약과 `data/network_probe_results/` 세션별 JSON

사전 릴리즈 제한:

- GitHub Actions Windows loopback 및 EXE 자체 점검 후 게시합니다.
- 같은 스위치의 Windows 11 PC 두 대에서 iperf 3회 중앙값 대비 ±10%를 확인한 뒤 정식 `v0.4.0`을 게시합니다.
- iperf는 비교 검증에만 사용하고 프로그램에 포함하거나 호출하지 않습니다.
- UDP와 Android 네이티브 TCP 클라이언트는 포함하지 않습니다.

## v0.3.0 - 2026-07-10

설치 없는 HTTP 지속 측정과 결과 기록을 추가한 릴리즈입니다.

추가된 내용:

- 기존 크기 기준 간편 측정과 별도의 HTTP 지속 측정 화면
- 방향별 3초 워밍 후 10초 또는 30초 본 측정
- HTTP 연결 1개/4개, 업로드/다운로드/전체 측정
- 평균·중앙·최소·최대·변동률, HTTP 응답시간, 1초 구간 그래프
- 서버 전체 동시 측정 1건 제한과 측정 취소·만료 처리
- `data/network_check_session_log.csv` 요약과 `data/network_check_results/` 세션별 JSON
- Windows 11 Edge/Chrome과 Android Chrome 반응형 화면
- 업로드 CSV 기록 실패 시 새로 생긴 고아 파일을 정리하는 안정성 개선

제한사항:

- 지속 측정은 브라우저 HTTP 응용 전송 성능입니다.
- TCP 재전송·CWND나 UDP 손실·지터를 측정하는 iperf 대체 기능은 포함하지 않습니다.
- TCP·UDP 정밀 측정 확장은 당시 후속 계획이었습니다.

## v0.2.2 - 2026-07-09

네트워크 체크 표시와 운영 안전장치 개선 릴리즈입니다.

개선된 내용:

- 진행 중 속도를 `평균 속도`와 `구간 속도`로 분리
- 속도 값을 `Mbps`와 `MB/s`로 함께 표시
- 측정 중 취소 버튼 추가
- `1024MB` 측정 시작 전 확인창 추가
- 취소 후 다시 측정할 수 있도록 버튼 상태 복구

## v0.2.1 - 2026-07-09

네트워크 체크 업로드 측정 안정화 릴리즈입니다.

수정된 문제:

- Edge/Chrome + HTTP/1.1 환경에서 업로드 측정이 `Failed to fetch`로 실패할 수 있던 문제를 수정
- 업로드 진행률이 10%에서 멈춘 것처럼 보일 수 있던 문제를 수정
- 브라우저 요청 스트리밍 대신 1MB 조각 단위 일반 POST 방식으로 변경
- 진행률을 서버가 받은 조각 기준으로 갱신하도록 조정
- 업로드 측정 실패 시 어느 단계에서 실패했는지 더 구체적으로 표시

## v0.2.0 - 2026-07-09

사내 업로드 도구에 네트워크 체크 모드를 추가했습니다.

추가된 기능:

- 상단 탭으로 `파일 업로드`와 `네트워크 체크` 모드를 분리
- 현재 PC와 서버 PC 사이의 업로드/다운로드 전송 속도 측정
- `10MB`, `50MB`, `100MB`, `500MB`, `1024MB` 테스트 크기 제공
- 업로드만, 다운로드만, 전체 측정 실행
- 측정 중 진행률과 현재 속도 표시
- 테스트 데이터는 파일로 저장하지 않고 스트리밍 후 폐기
- 네트워크 체크 결과를 `data/network_check_log.csv`에 별도로 기록
- 기존 `data/upload_log.csv` 업로드 기록과 속도 측정 기록 분리

제한사항:

- 인터넷 회선 속도 측정이 아니라 현재 PC와 사내 업로드 서버 PC 사이의 전송 속도 측정입니다.
- 1024MB 측정은 사내망과 서버 PC에 부하를 줄 수 있습니다.
- 스트리밍 업로드는 Windows 환경의 Edge/Chrome 기준으로 검증합니다.

## v0.1.0 - 2026-07-09

초기 사내 장애처리용 미니 업로드 도구입니다.

포함된 기능:

- Windows PC에서 `run.bat`로 실행하는 Python Flask 웹앱
- Python 설치 없이 실행할 수 있는 Windows EXE ZIP Release asset
- 파일 업로드, 선택 메모 입력, 저장 하위 폴더 지정
- `config.ini` 기반 `BASE_URL`, 포트, 저장 기준 폴더, 삭제 허용 IP 설정
- `BASE_URL` 우선 다운로드 링크 생성, 미설정 시 서버 PC IP 기반 링크 생성
- `localhost` 또는 `127.0.0.1` 링크가 생성될 때 다른 PC 사용 불가 경고 표시
- `/download/<upload_id>` 형식의 ID 기반 직접 다운로드 링크
- 같은 이름의 파일이 이미 있으면 먼저 경고하고, 사용자가 확인하면 ID를 붙여 저장
- 최근 50개 업로드 목록 표시
- 설정된 허용 IP에서만 파일과 CSV 기록 삭제
- `data/upload_log.csv` 기반 업로드 기록
- DB, 로그인, 권한관리, 수신자 지정, 만료일, 관리자 페이지 제외

## Release 전 문서 점검

GitHub에 push하거나 Release를 준비하기 전에 아래 문서를 함께 확인합니다.

- `README.md`: 설치, 실행, 설정, 방화벽, 업로드, 다운로드, 삭제, 제한사항
- `RELEASE_NOTES.md`: 릴리즈 설명 규칙과 현재 배포 기준
- `CHANGELOG.md`: 구현된 변경과 제외된 항목 구분

다음 항목이 바뀌면 문서도 같은 변경에 포함합니다.

1. 실행 방법 또는 Python 요구사항
2. `config.ini` 키와 기본값
3. 서버 포트, `BASE_URL`, IP 자동 감지 방식
4. 저장 폴더 정책과 허용 경로
5. CSV 필드 또는 기록 위치
6. 삭제 허용 IP와 삭제 동작
7. 업로드 중복 파일 처리
8. GitHub Release asset 또는 배포 ZIP 정책

## GitHub Release / Asset 계약

현재 GitHub Release는 태그 기준으로 생성하고, Windows 실행 ZIP은 GitHub Actions에서 빌드해 업로드합니다.

- 태그 형식: 사전 릴리즈는 `v0.5.2-rc.1`, 정식 릴리즈는 `v0.5.2`처럼 관리합니다.
- Release 제목: `v0.5.2 - 사내 업로드 사용성 및 안정성 개선`
- Release 본문: 포함 기능, 제외 항목, 검증 명령, 실행 방법, asset 정책을 한국어로 적습니다.
- 직접 업로드하는 Release asset: `internal-upload_v0.5.2_windows.zip`, `.zip.sha256`
- SHA256 checksum은 별도 asset과 Release 본문에 기록합니다.
- 같은 태그의 GitHub Release가 이미 있으면 실패 처리하며 기존 asset을 덮어쓰지 않습니다.

GitHub가 자동으로 표시하는 `Source code (zip)` / `Source code (tar.gz)`는 tag 기준 소스 아카이브입니다. 일반 사용자는 `internal-upload_v0.5.2_windows.zip`을 다운로드합니다.

ZIP 내부 구조:

- `InternalUploadServer.exe`, 서버 `_internal/`
- `start_internal_upload.cmd`
- `client-template/NetworkProbeClient.exe`, 클라이언트 `client-template/_internal/`
- `config.ini`
- `README_START_HERE_KO.txt`
- `README.md`, `RELEASE_NOTES.md`, `CHANGELOG.md`
- `SECURITY_REVIEW_KO.md`, `security_manifest.json`, `sbom.cdx.json`, `SHA256SUMS.txt`
- `data/upload_log.csv`
- `data/network_check_log.csv`
- `data/network_check_session_log.csv`
- `data/network_check_results/README_RESULTS_KO.txt`
- `data/network_probe_log.csv`
- `data/network_probe_results/README_RESULTS_KO.txt`
- `uploads/README_UPLOADS_KO.txt`

## 검증 기준

Release 또는 GitHub push 전에 다음 검증을 실행합니다.

```powershell
python -m compileall app_version.py app.py bounded_server.py probe_client.py startup_ports.py runtime_stability.py upload_transactions.py measurement_transactions.py network_sustained.py sustained_excel.py excel_report.py network_measurement.py result_storage.py network_probe tests tools
python -m pytest -q
```

macOS 작업 환경에서는 다음 명령을 사용합니다.

```bash
.venv/bin/python -m compileall app_version.py app.py bounded_server.py probe_client.py startup_ports.py runtime_stability.py upload_transactions.py measurement_transactions.py network_sustained.py sustained_excel.py excel_report.py network_measurement.py result_storage.py network_probe tests tools
.venv/bin/python -m pytest -q
```

Windows Release ZIP 검증은 GitHub Actions `windows-latest`에서 실행합니다.

```powershell
python -m compileall app_version.py app.py bounded_server.py probe_client.py startup_ports.py runtime_stability.py upload_transactions.py measurement_transactions.py network_sustained.py sustained_excel.py excel_report.py network_measurement.py result_storage.py network_probe tests tools
node --check static/network_check.js
node --check static/network_sustained.js
node --check static/network_probe.js
node --check static/throughput_chart.js
node --check static/operations_dashboard.js
python -m pytest -q
python tools\run_stability_fault_suite.py
pwsh -NoProfile -File .\tools\build_windows_release.ps1 -Version v0.5.2
dist\internal-upload_v0.5.2_windows\InternalUploadServer.exe --smoke-check
dist\internal-upload_v0.5.2_windows\InternalUploadServer.exe --probe-self-check
dist\internal-upload_v0.5.2_windows\client-template\NetworkProbeClient.exe --self-check
python tools\verify_release_zip.py --zip dist\internal-upload_v0.5.2_windows.zip --version v0.5.2
```

## 작성하지 않을 내용

- 실제 사내 IP, 서버 PC 이름, 사용자 계정, 비밀번호
- 실제 장애자료 파일명, 메모, 업로드 CSV 기록
- 고객명, 사이트명, 내부망 식별자
- 아직 구현하지 않은 로그인, 만료일, 관리자 페이지
