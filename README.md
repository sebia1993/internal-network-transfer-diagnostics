# 사내 파일 전송 및 네트워크 체크

사내 장애처리용 파일 업로드와 HTTP/TCP 네트워크 측정 도구입니다. 브라우저에서 파일과 메모를 올려 다운로드 링크를 만들고, 같은 화면에서 서버 상태와 최근 측정 표본을 확인할 수 있습니다. 운영 요약은 장비 인벤토리나 장애 티켓 시스템이 아닙니다.

## 실행 방법

일반 사용자는 GitHub Release에서 Windows 실행 ZIP을 받습니다.

1. `internal-upload_v0.5.1_windows.zip`을 다운로드합니다.
2. Windows 서버 PC의 원하는 폴더에 ZIP을 완전히 압축 해제합니다.
3. `start_internal_upload.cmd`를 더블클릭합니다.
4. 콘솔에 표시된 실제 접속 주소를 브라우저에서 엽니다. 기본 주소는 아래와 같습니다.

```text
http://127.0.0.1:8000
```

다른 PC에서 접속하려면 서버 PC의 사내 IP를 사용합니다.

```text
http://서버PC-IP:8000
```

설정된 포트가 다른 프로그램에서 사용 중이면 `8001`부터 순서대로 빈 포트를 확인한 뒤 변경 여부를 묻습니다. Enter 또는 `Y`로 승인하면 실제 바인딩에 성공한 포트를 `config.ini`에 저장하고 그 주소를 표시합니다. `N`을 입력하면 설정을 바꾸지 않고 서버를 시작하지 않으며 `WEB_PORT_CHANGE_DECLINED`와 종료 코드 2를 반환합니다.

프로그램은 Windows 방화벽을 자동 조회하거나 변경하지 않고 관리자 권한도 요청하지 않습니다. 다른 PC에서 접속할 수 없으면 콘솔에 표시된 현재 포트와 수동 방화벽 허용 명령을 관제·보안 정책에 맞춰 확인합니다.

서버와 TCP 클라이언트는 기능이 분리된 코드서명 미적용 EXE입니다. Windows SmartScreen 또는 보안 제품 경고가 표시될 수 있으며, 배포 ZIP의 `SECURITY_REVIEW_KO.md`, `security_manifest.json`, `SHA256SUMS.txt`와 `sbom.cdx.json`으로 출처와 예상 동작을 검토할 수 있습니다.

## 소스에서 실행

Windows 서버 PC에서:

```bat
run.bat
```

소스에서 실행하는 방식은 Python이 설치된 개발/운영 PC용입니다. Python 없이 실행하려면 Release ZIP을 사용하세요.

## 설정

`config.ini`에서 운영 값을 수정합니다.

```ini
[app]
CONFIG_VERSION=2
HOST=0.0.0.0
PORT=8000
BASE_URL=
STORAGE_ROOT=uploads
DELETE_ALLOWED_IPS=127.0.0.1,::1
RECENT_LIMIT=50

[network_probe]
ENABLED=true
PORT=5201
```

- `BASE_URL`: 다운로드 링크 기준 주소입니다. 비워 두거나 사용자정보·query·fragment가 없는 `http://` 또는 `https://` URL을 사용합니다. 예: `http://10.10.10.25:8000`
- `STORAGE_ROOT`: 비어 있지 않은 파일 저장 기준 폴더입니다. 상대경로면 프로젝트 폴더 기준이며 일반 파일 경로는 허용하지 않습니다.
- `DELETE_ALLOWED_IPS`: 삭제 버튼과 삭제 요청을 허용할 개별 IPv4/IPv6 주소 목록입니다. CIDR은 지원하지 않습니다.
- `RECENT_LIMIT`: 화면에 표시할 최근 업로드 개수이며 허용 범위는 1~10000입니다.
- `CONFIG_VERSION`: 설정 마이그레이션 표식입니다. 사용자가 직접 수정할 필요가 없습니다.
- `network_probe.ENABLED`: Windows TCP 전송 성능 측정 서버를 켭니다. 기본값은 `true`이며 필요하면 `false`로 끌 수 있습니다.
- `network_probe.PORT`: TCP 측정 데이터 포트입니다. 기본값은 `5201`이며 웹 `PORT`와 달라야 합니다.

`BASE_URL`이 비어 있으면 프로그램이 서버 PC의 사내 IP를 자동 감지해 링크를 만듭니다. 링크가 `localhost` 또는 `127.0.0.1`로 생성되면 다른 PC에서는 사용할 수 없으므로 화면에 경고가 표시됩니다.

`config.ini`가 UTF-8로 읽히지 않거나 INI 형식이 아니거나, `HOST`, `PORT`, `BASE_URL`, `STORAGE_ROOT`, `DELETE_ALLOWED_IPS`, `RECENT_LIMIT`, `ENABLED`, `CONFIG_VERSION`에 허용되지 않은 값을 입력하면 프로그램은 임의의 기본값으로 실행하지 않습니다. 콘솔에 잘못된 섹션·항목과 허용 범위를 표시한 뒤 종료하며, 입력값 전체나 비밀값은 출력하지 않습니다. 현재 서버는 IPv4 bind만 지원하므로 `HOST`에는 IPv4 주소 또는 포트가 없는 호스트 이름을 사용합니다.

웹 포트 충돌로 포트 변경을 승인하면 `PORT`는 새 값으로 저장됩니다. `BASE_URL`이 기존 웹 포트를 사용하고 있을 때만 해당 포트도 함께 변경합니다. 별도 프록시 포트처럼 다른 포트를 사용 중인 `BASE_URL`은 유지하고 콘솔에 주의 문구를 표시합니다. 이미 같은 서버가 실행 중이면 새 서버를 중복 실행하지 않고 기존 주소만 안내합니다.

서로 다른 웹 포트를 사용하더라도 같은 `data` 폴더를 공유하는 서버는 동시에 실행할 수 없습니다. 프로그램이 `data/.internal-upload.instance.lock`을 운영체제 수준으로 잠그며, 정상 종료와 비정상 프로세스 종료 때 잠금은 자동 해제됩니다.

웹 서버는 동시에 처리하는 요청을 최대 32개로 제한합니다. 30초 동안 데이터가 오가지 않는 연결은 종료하고, 처리 한도를 넘은 연결에는 잠시 후 다시 시도할 수 있도록 HTTP 503을 반환합니다. 정상적으로 데이터가 계속 전송되는 대용량 업로드·다운로드에는 30초 전체 실행 제한을 적용하지 않습니다. Ctrl+C로 종료하면 새 요청을 더 이상 처리하지 않고 진행 중인 웹 요청이 끝나기를 최대 30초 기다립니다. 남은 느린 연결에는 소켓 종료를 요청하고 미완료 측정을 실패 또는 취소 결과로 정리한 뒤 2초를 더 기다립니다. 그래도 요청 스레드가 남으면 데이터 잠금을 먼저 풀지 않고 진단 로그를 디스크에 반영한 뒤 종료 코드 2로 프로세스를 즉시 종료합니다. 이 예외 경로에서는 운영체제가 스레드·소켓·데이터 잠금을 함께 회수하며, 다음 시작 때 업로드 트랜잭션과 CSV 무결성 복구를 다시 수행합니다.

TCP 전송 성능 측정 포트가 사용 중이면 다음 99개 포트에서 빈 포트를 찾아 변경 여부를 묻습니다. 승인한 포트는 TCP 서버 바인딩 성공 후 `[network_probe] PORT`에 저장합니다. 변경을 거절하거나 사용할 포트가 없으면 파일 업로드 웹 서버와 HTTP 측정은 계속 실행하고 TCP 측정만 사용할 수 없게 표시합니다. 이전 릴리즈의 `ENABLED=false` 설정은 `rc.6` 첫 실행에서 한 번 `true`로 전환되며, 이후 사용자가 다시 `false`로 설정하면 그대로 유지됩니다.

## 사용 방식

1. 파일을 선택합니다.
2. 필요한 경우 저장 하위 폴더를 입력합니다.
3. 필요한 경우 메모를 입력합니다.
4. 업로드 후 생성된 다운로드 링크를 공유합니다.

저장 하위 폴더는 `STORAGE_ROOT` 아래만 허용합니다. `C:\temp` 같은 절대경로나 `..` 경로는 차단합니다.

같은 저장 위치에 같은 이름의 파일이 있으면 먼저 경고가 표시됩니다. 그래도 업로드하려면 파일을 다시 선택하고 `같은 이름이 있으면 ID를 붙여 저장`을 체크하세요.

실행파일, 스크립트, 바로가기, 드라이버, 설치 패키지, 매크로 포함 Office 문서와 디스크 이미지는 업로드할 수 없습니다. 확장자를 바꾼 Windows PE 파일도 `MZ` 헤더로 차단합니다. 일반 문서, PCAP, EVTX와 ZIP 같은 압축파일은 허용하지만 압축파일 내부 내용은 검사하지 않으며 파일 크기 제한도 없습니다. 대신 서버 운영을 위해 최소 1GB의 디스크 여유 공간을 남깁니다. 파일 업로드는 최대 4건을 동시에 처리하고, 진행 중인 요청들의 아직 기록하지 않은 예상 용량을 합산해 디스크 여유를 예약합니다. 한도를 넘으면 HTTP 503, 합산 예약 후 공간이 부족하면 HTTP 507로 거절합니다. 업로드 중에도 8MB마다 남은 공간을 다시 확인하며, 공간이 부족하면 임시 파일을 제거합니다. 다운로드 응답은 브라우저 실행 해석을 줄이기 위해 항상 첨부파일과 `application/octet-stream`, `nosniff`로 제공합니다.

## 네트워크 체크

상단 `네트워크 체크` 탭에서 현재 PC와 서버 PC 사이의 업로드/다운로드 전송 속도를 확인할 수 있습니다.

측정 방식은 `HTTP 전송 측정`과 `TCP 전송 성능 측정` 두 가지입니다. HTTP 측정 안에서 종료 기준을 `데이터량` 또는 `측정 시간`으로 선택합니다.

### HTTP 전송 측정 - 데이터량 기준

- `10MB`, `50MB`, `100MB`, `500MB`, `1024MB` 중 측정 데이터량을 선택합니다.
- 선택한 데이터량을 모두 전송할 때까지 HTTP 처리량을 측정합니다.
- 업로드, 다운로드, 전체 측정을 실행합니다.
- 진행률, 현재 평균 속도와 최근 전송 속도를 `Mbps`와 `MB/s`로 표시합니다.
- 완료 후 전송한 데이터, 걸린 시간, 최종 평균 속도, 초당 파일 전송량과 현재 속도 기준 1GB 예상 시간을 표시합니다.
- 업로드는 HTTP/1.1에서 안정적으로 동작하는 1MB 일반 POST 조각을 사용합니다.
- `1024MB` 측정은 시작 전에 부하 확인창을 표시합니다.

### HTTP 전송 측정 - 측정 시간 기준

- 별도 클라이언트 프로그램 설치 없이 Windows 11 Edge/Chrome과 Android Chrome에서 실행합니다.
- 선택한 시간 동안 HTTP 전송을 계속해 처리량과 변동을 측정합니다.
- 각 방향마다 3초 워밍 후 10초 또는 30초를 본 측정합니다.
- 브라우저와 서버 사이의 HTTP 연결 1개로 측정합니다.
- 측정 중에는 현재 단계, 남은 시간, 전체 진행률과 최근 3초 평균 속도만 표시합니다.
- 완료 후에는 업로드·다운로드 평균 속도와 속도 변동률을 먼저 표시하고 방향별 1초 속도 그래프를 제공합니다.
- 그래프는 0Mbps부터 시작하며 평균선과 최저·최고를 표시합니다. 마우스·터치·키보드로 각 초의 Mbps와 MB/s를 확인할 수 있습니다.
- HTTP 응답시간, 중앙값, 최저·최고, 전송량과 측정 조건은 `기술 상세 보기`에서 확인합니다.
- 응답시간 확인부터 모든 워밍업·본 측정이 끝날 때까지 전체 진행률이 한 번만 0%에서 100%로 증가합니다.
- 실패 또는 취소 시에는 중단된 위치의 진행률을 유지합니다.
- 전체 측정에서 한 방향만 끝난 뒤 다른 방향이 실패하면 완료된 방향의 유효한 결과를 `부분 완료`로 보존하고 실패한 방향만 다시 측정하도록 안내합니다.
- 서버 전체에서 한 번에 하나의 HTTP 시간 기준 측정만 허용합니다.
- 30초 측정은 시작 전에 부하 확인창을 표시합니다.
- 완료·실패·취소된 세션의 `결과 요약`, `속도 변화`와 1초 그래프를 담은 Excel 결과를 현재 측정 PC에서 받을 수 있습니다.
- Excel 시각은 서버 PC 설정과 관계없이 한국 표준시(KST)로 표시하고 내부 세션 ID는 시트와 파일명에 노출하지 않습니다.

### TCP 전송 성능 측정

TCP 전송 성능 측정은 서버 전용 `InternalUploadServer.exe`와 측정 전용 `NetworkProbeClient.exe` 사이에서 별도 TCP 연결을 만들어 처리량을 측정합니다. 브라우저는 측정 시작·취소와 결과 표시만 담당합니다.

서버 PC에서:

1. `start_internal_upload.cmd`를 실행합니다. TCP 전송 성능 측정은 기본으로 함께 시작됩니다.
2. 콘솔에 표시된 실제 TCP 측정 포트를 확인합니다. 기본값은 `5201`입니다.
3. 다른 PC에서 연결되지 않으면 콘솔에 표시된 명령으로 해당 TCP 포트를 Windows 방화벽에서 허용합니다.

측정 대상 Windows PC에서:

1. 서버 웹 화면을 현재 서버 PC의 사내 IP 또는 PC 이름으로 엽니다.
2. `TCP 전송 성능 측정`에서 `Windows 클라이언트 ZIP 받기`를 누릅니다.
3. 받은 ZIP을 완전히 압축 해제하고 `NetworkProbeClient.exe`를 실행합니다.
4. 주소 입력 없이 자동 등록된 `PC 이름 · 접속 IP`를 웹 화면에서 선택합니다.

클라이언트는 등록 직후와 대기 중 약 20초마다 서버의 실제 TCP 측정 포트를 자동 점검합니다. 웹 화면에 `준비 완료`가 표시된 클라이언트만 측정을 시작할 수 있습니다. 연결이 실패하면 서버 콘솔의 TCP 포트와 Windows 방화벽 인바운드 허용을 확인하세요. 자동 점검은 측정 데이터를 보내지 않고 작은 제어 메시지만 주고받습니다.

클라이언트 ZIP은 접속 중인 서버 주소와 웹 포트를 `client-config.json`에 자동으로 포함합니다. 서버 웹 화면을 `localhost` 또는 `127.0.0.1`로 열었다면 프로그램이 감지한 서버 PC의 사내 IPv4를 대신 사용합니다. TCP 데이터 포트는 등록 후 서버가 자동 전달하므로 해당 포트만 바뀐 경우에는 ZIP을 다시 받을 필요가 없습니다. 서버 IP 또는 웹 포트가 바뀐 경우에만 ZIP을 다시 받으세요. 클라이언트 ZIP에는 서버 실행 파일, CMD, `config.ini`, 인증 토큰이나 세션 정보가 들어가지 않습니다. `client-manifest.json`과 웹 화면에서 클라이언트 EXE SHA256을 확인할 수 있습니다.

웹 다운로드는 TCP 측정 서버가 정상이며 Windows Release 서버로 실행할 때만 사용할 수 있습니다. 소스 실행 환경에는 Windows 클라이언트 바이너리가 없으므로 클라이언트 ZIP 다운로드를 제공하지 않습니다.

서버와 클라이언트 릴리즈 버전은 웹 화면에 함께 표시됩니다. `v0.5.1`은 TCP 프로토콜 `v2`를 유지하므로 `v0.4.3` 이후 클라이언트와 프로토콜상 호환되지만, 실행 역할이 분리된 최신 클라이언트 ZIP 사용을 권장합니다. `v0.4.2` 및 이전 클라이언트는 서버 웹 화면에서 최신 ZIP을 다시 받아야 합니다.

지원 범위:

- 업로드, 다운로드, 전체 순차 측정
- 일반 측정은 TCP 1개 스트림을 사용하며 `고급 비교 측정`에서만 4개 스트림을 선택
- 방향별 3초 워밍업 후 10초 또는 30초 본 측정
- 업로드 `측정 PC → 서버`, 다운로드 `서버 → 측정 PC` 경로별 실제 수신 평균·중앙값·최소·최대 속도
- 웹 요약에는 업로드·다운로드 실제 수신 평균 속도만 표시
- 완료 직후 업로드·다운로드 실제 수신 속도 그래프를 각각 표시하고, RTT·재전송·중앙값·최저·최고·측정 조건은 `기술 상세 보기`에서 확인
- Windows TCP_INFO가 제공될 때 RTT, 최소 RTT와 재전송 바이트 표시
- 측정 취소, 연결 끊김 감지, 서버 전체 동시 네트워크 측정 1건 제한
- 관리자용 `결과 요약`, 실제 수신 기준 `속도 변화`, CWND를 포함한 `기술 상세` 세 시트의 Excel 결과 다운로드
- TCP Excel도 한국 표준시(KST)를 사용하고 세션 ID를 숨기며, 10초는 모든 지점, 30초는 5초 간격과 최저·최고 값만 그래프에 표시

TCP 제어 API는 요청 JSON 본문을 64KiB로 제한하며 길이를 미리 알 수 없는 전송도 실제 읽은 크기로 검사합니다. 서버 메모리에 유지하는 등록 클라이언트는 만료 정리 후 최대 256개이고, 결과의 스트림·구간 개수와 전송량 합계·수치 범위를 검증합니다. 제한을 넘긴 요청은 각각 HTTP 413, 429 또는 형식 오류로 거절하며 성공 측정으로 저장하지 않습니다.

TCP 클라이언트는 서버로만 연결하므로 클라이언트 PC의 인바운드 포트를 열 필요가 없습니다. 상세 TCP 통계를 조회할 수 없는 환경에서는 값을 추정하지 않고 `운영체제에서 제공하지 않음`으로 표시합니다. 측정하지 않은 방향은 `측정 안 함`으로 구분합니다.

HTTP 데이터량·측정 시간 기준은 브라우저와 Flask 서버 사이의 HTTP 응용 전송 성능입니다. TCP 전송 성능 측정은 브라우저를 데이터 경로에서 제외하지만 자체 프로토콜이므로 iperf 클라이언트·서버와 호환되지 않습니다. 기준 속도가 설정되지 않으므로 결과를 정상·비정상으로 자동 판정하지 않습니다. 모든 측정값에는 단말 CPU, NIC, Wi-Fi와 서버 PC 성능이 함께 반영됩니다. 설계 대상은 1Gbps 이하 사내망입니다.

테스트 데이터는 서버에 파일로 저장하지 않고 측정 후 폐기합니다. `1024MB` 측정은 사내망과 서버 PC에 부하를 줄 수 있으므로 장애 상황에서 필요할 때만 사용하세요.

## 기록과 삭제

업로드 기록은 `data/upload_log.csv`에 저장됩니다. CSV에는 업로드일시, 원본파일명, 저장파일명, 저장경로, 메모, 다운로드 링크가 남습니다. 업로드 내용은 동일 폴더의 전용 임시 파일에 완전히 저장된 후 최종 이름으로 확정됩니다. 파일 확정과 CSV 기록 사이에는 작은 전용 트랜잭션 표식을 남겨, 그 순간 프로세스가 종료돼도 다음 시작 때 누락된 기록을 복구합니다. 삭제도 CSV 갱신과 실제 파일 삭제를 같은 방식으로 복구합니다. Windows에서는 파일 확정과 CSV·JSON·설정·트랜잭션 파일 교체가 디스크 반영을 기다리는 Win32 write-through 방식으로 수행됩니다. 업로드 기록은 서버 메모리에 색인해 화면 표시와 다운로드 때 CSV 전체를 반복해서 읽지 않으며, 외부에서 CSV가 변경되면 파일 상태를 확인해 자동으로 다시 읽습니다. 업로드 파일과 해당 기록에는 자동 보관 기한을 적용하지 않습니다.

비정상 종료 뒤에는 프로그램을 다시 시작해 오류 없이 서버 주소가 표시되고 초기 복구가 끝난 것을 확인할 때까지 `STORAGE_ROOT`, `data/upload_log.csv`, `data/upload_transactions/`의 파일명·내용을 수동 변경하거나 같은 저장 경로를 재사용하지 마세요. 미완료 트랜잭션과 다른 업로드 기록이 같은 경로를 가리키면 프로그램은 새 파일을 임의로 삭제하거나 덮어쓰지 않고 시작을 중단합니다.

HTTP 데이터량 기준 측정은 `data/network_check_log.csv`에 저장됩니다. HTTP 측정 시간 기준 요약은 `data/network_check_session_log.csv`, 상세 원본은 `data/network_check_results/<session_id>.json`에 저장됩니다. 화면의 HTTP Excel 파일은 이 원본에서 요청할 때 메모리로 생성하므로 서버에 별도 저장하지 않습니다. TCP 전송 성능 측정은 `data/network_probe_log.csv`와 `data/network_probe_results/<session_id>.json`에 저장되며 TCP Excel도 저장된 JSON에서 요청 시 메모리로 생성합니다. 운영 CSV와 JSON은 GitHub에 올리지 마세요.

HTTP 측정 시간 기준과 TCP 결과는 상세 JSON과 요약 CSV 사이에 `data/measurement_transactions/`의 작은 트랜잭션 표식을 사용합니다. JSON이 확정된 직후 또는 여러 CSV 행 중 일부만 디스크에 반영된 순간 프로세스가 종료돼도 다음 시작 때 표식의 JSON 해시를 확인하고 상세 JSON에서 예상 CSV 행을 다시 만들어 모든 필드를 대조한 뒤 누락된 행만 한 번 복구합니다. 일반 저장 오류로 rollback을 시작한 경우도 별도 상태를 먼저 확정해 CSV 정리나 JSON 삭제 한쪽만 실패하더라도 다음 시작에서 정리를 완료합니다. 같은 세션의 행이 중복되거나 내용이 다르거나 JSON이 바뀐 경우에는 정상으로 추정하지 않고 시작을 중단합니다. 표식 정리만 실패하면 같은 종류의 다음 측정을 세션·gate·watchdog·네트워크 전송을 시작하기 전에 `MEASUREMENT_RECOVERY_PENDING` 503으로 거절하고 서버 재시작을 안내해 오래된 상세 JSON이 후속 보관 정리에서 삭제되지 않게 합니다. 측정 도중 표식이 새로 생기는 경쟁 조건에서도 같은 오류 코드를 보존합니다. 초기 복구가 끝날 때까지 이 폴더와 측정 JSON·CSV를 수동으로 수정하지 마세요.

측정 CSV가 10,000행을 넘으면 최근 5,000행은 기존 파일에 유지하고 이전 행은 `data/archives/<CSV이름>/YYYY-MM.csv`에 월별로 보관합니다. HTTP 시간 기준과 TCP 상세 JSON은 각각 최신 1,000건을 유지하며 그보다 오래된 상세 JSON은 제거하지만 CSV 요약은 활성 CSV 또는 월별 보관 파일에 남습니다. 이 제한은 업로드 파일과 `upload_log.csv`에는 적용하지 않습니다.

삭제는 `DELETE_ALLOWED_IPS`에 등록된 IP에서 접속했을 때만 가능합니다. 삭제하면 서버에 저장된 파일과 CSV 기록이 함께 삭제됩니다.

프로그램은 시작할 때 네 종류의 운영 CSV 헤더와 레코드 형식을 검사합니다. 전원 차단 등으로 파일 끝의 마지막 레코드만 미완성인 경우 원본을 `*.recovery-날짜-식별값.bak`으로 먼저 백업한 뒤 해당 레코드만 제거합니다. CSV별 복구 백업은 최신 5개를 유지합니다. 헤더 또는 중간 레코드가 손상된 경우에는 임의 복구로 정상 기록을 지우지 않고 서버 시작을 중단합니다. 비정상 종료로 남은 업로드 임시 파일은 예약 소유 프로세스가 종료된 것을 확인한 뒤 다음 시작 때 제거합니다.

다운로드와 삭제 때는 CSV의 `storage_path`가 현재 `STORAGE_ROOT` 내부인지 다시 검사합니다. CSV가 손상되거나 수동 편집돼 기준 폴더 밖을 가리키면 해당 경로의 파일을 다운로드하거나 삭제하지 않습니다.

운영 진단 로그는 `data/diagnostics/internal-upload.log`에 기록합니다. 파일 하나는 최대 2MB이며 이전 로그는 최대 5개까지 순환 보관합니다. 진단 로그에는 업로드 파일명, 메모, 인증 토큰과 전송 내용은 기록하지 않습니다.

예상하지 못한 업로드·삭제·측정 결과 저장 실패는 기본 Python 오류 화면으로 넘기지 않고 `UPLOAD_PROCESSING_FAILED`, `DELETE_PROCESSING_FAILED`, `RESULT_WRITE_FAILED` 같은 안정된 오류 코드와 다음 조치를 표시합니다. TCP·웹 포트 bind, 진단 로그 초기화, 변경 포트 저장과 클라이언트 ZIP 처리 실패도 `TCP_BIND_FAILED`, `WEB_BIND_FAILED`, `DIAGNOSTIC_LOG_INIT_FAILED`, `CLIENT_PACKAGE_BUILD_FAILED`처럼 구분합니다. 상세 예외 문구와 설정의 절대 경로는 화면, 상태 API와 운영 요약에 내보내지 않으며 진단 로그에는 단계 이벤트와 예외 종류만 남깁니다.

`GET /api/health`는 다음 상태를 `checks` 항목으로 반환합니다.

- 업로드 폴더와 메타데이터 폴더의 쓰기 가능 여부와 남은 공간
- 네 종류 CSV의 형식 정상 여부
- TCP 측정 서버의 활성화·실행 상태
- 현재 네트워크 측정의 점유 시간, 장기 실행 경고와 강제 중단 콜백 실패 누적 횟수
- 현재 파일 업로드 수, 최대 동시 업로드 수와 남은 예약 용량
- 빠른 HTTP·시간 기준 HTTP·TCP 측정의 백그라운드 결과 저장 또는 CSV 보관 실패 누적 횟수

모든 항목이 정상이면 `status=ok`, 하나라도 운영 확인이 필요하면 HTTP 200을 유지하면서 `status=degraded`를 반환합니다. 디스크 쓰기와 CSV 전체 검사는 반복 호출 부하를 줄이기 위해 최대 5초 동안 서버 내부에서 재사용하고, TCP 실행 상태와 현재 측정 점유 상태는 매 요청마다 새로 확인합니다. 장기 실행 상한을 넘은 측정은 상태 점검 또는 새 측정 요청 때 취소를 요청하며, 실제 작업이 정리되기 전에는 잠금을 임의로 제거하지 않습니다. 강제 중단 과정에서 결과 저장 또는 정리 콜백이 실패하면 진단 로그에 `measurement_cancel_callback_failed`를 남기고 `checks.measurement.cancel_callback_failure_count`를 누적합니다. 이 값이 1 이상이면 측정 점유가 이미 풀렸더라도 `checks.measurement.status`와 전체 `status`를 `degraded`로 유지하므로 진단 로그 확인과 서버 재시작이 필요합니다.

`GET /api/operations-summary`는 활성 CSV의 최근 측정 결과를 유형별 최대 50건씩 읽어 다음 정보를 제공합니다.

- 최근 표본의 완료·취소·실패 결과 수
- 최근 실패·취소 항목의 측정 종류, 방향, 영향과 원인 분류별 권장 조치
- 같은 측정 방식·방향에서 완료·취소·실패가 실제로 바뀐 최근 기록과 현재 측정·파일 업로드 점유 상태
- TCP 측정 서버 사용 가능 여부와 읽지 못한 기록 종류

이 요약에는 클라이언트 IP, PC 이름, 세션 ID, 인증 토큰과 원시 오류 문구를 포함하지 않습니다. 측정 `완료`는 데이터 전송과 결과 저장이 끝났다는 뜻이며 속도가 운영 기준에 적합하다는 판정은 아닙니다. 과거 실패 표본도 현재 미조치 장애를 뜻하지 않습니다. 기존 CSV·JSON은 변경하지 않으며 상세 근거가 필요하면 운영 권한 범위에서 원본 기록과 진단 로그를 별도로 확인합니다.

HTTP 데이터량 기준 업로드 세션은 마지막 청크 이후 15분 동안 활동이 없을 때 실패로 종료합니다. 전송 청크가 계속 도착하면 전체 시간이 15분을 넘어도 세션을 유지하지만, 시작 후 절대 상한 30분에 도달하면 활동 여부와 관계없이 실패로 종료합니다. HTTP 시간 기준과 TCP 측정이 5분 상한을 넘으면 해당 측정 관리자에 취소를 요청하고, 기존 작업이 실제로 정리돼 잠금을 반납하기 전에는 새 측정을 시작하지 않습니다. 따라서 만료 시점에 두 측정이 겹치지 않습니다.

HTTP 시간 기준과 TCP 결과 JSON은 권한·형식 검증과 원문 읽기를 한 번에 수행합니다. 오래된 결과 정리와 다운로드가 겹치거나 파일이 잘못된 UTF-8이면 절대 경로나 traceback을 노출하는 HTML 500 대신 `RESULT_READ_FAILED` JSON 오류를 반환합니다. 처음부터 없는 결과의 404와 HTTP 시간 기준 결과의 요청 IP 제한 403은 유지합니다.

현재 구조, 사용자별 평가, 안정성 근거, P0/P1/P2 목록과 단계별 검증 계획은 [v0.5.1 한국어 프로젝트 진단 보고서](https://github.com/sebia1993/-/blob/v0.5.1/docs/PROJECT_DIAGNOSTIC_AND_IMPROVEMENT_PLAN_KO.md)에 정리했습니다. 소스 저장소에서는 `docs/PROJECT_DIAGNOSTIC_AND_IMPROVEMENT_PLAN_KO.md` 경로로도 확인할 수 있습니다.

## GitHub 이력관리와 릴리즈 문서

GitHub에 push하거나 Release를 준비하기 전에는 아래 파일을 함께 확인합니다.

- `README.md`: 실행 방법, 설정값, 방화벽, 업로드/다운로드/삭제 방법
- `RELEASE_NOTES.md`: 릴리즈 전 점검 기준과 배포 asset 정책
- `CHANGELOG.md`: 사용자 관점 변경사항
- `AGENTS.md`: Codex 작업 규칙과 문서 최신화 기준

현재 정식 Release는 `v0.5.1`이며 실행용 asset은 `internal-upload_v0.5.1_windows.zip`과 SHA256 파일입니다. GitHub가 자동으로 표시하는 `Source code (zip)` / `Source code (tar.gz)`는 소스 아카이브이며 일반 실행용 ZIP이 아닙니다.

Windows ZIP은 PyInstaller `onedir` 포터블 구조이므로 EXE 옆의 `_internal` 폴더를 이동하거나 삭제하면 실행되지 않습니다. 서버는 시작 과정에서 PowerShell을 호출하지 않으며 레지스트리, 시작프로그램, 예약 작업과 Windows 방화벽을 변경하지 않습니다. 코드서명 미적용, 사내망 전체 무인증 접근, HTTP/TCP 평문 토큰 전송, 요청 Host를 이용한 클라이언트 ZIP 접속 주소 생성, 파일 크기 무제한, 압축파일 내부 미검사와 TCP 클라이언트 장기 폴링은 남아 있는 운영 위험입니다. 신뢰 VLAN과 ACL 밖에 직접 노출하지 마세요.

실제 사내 IP, 서버 PC 이름, 계정, 비밀번호, 업로드 자료, 장애 메모, 고객 정보는 문서와 Git 커밋에 넣지 않습니다. 저장소에 추적된 `config.ini`는 기본 예시값, `data/*.csv` 네 파일은 헤더 1행만 유지해야 하며 자동 테스트가 이 조건을 검사합니다. 운영 후 같은 폴더에서 개발할 때는 `git add -A` 전에 반드시 이 파일들을 확인하세요.

## 개발 검증

```bat
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\pytest -q
python tools\run_stability_fault_suite.py
python tools\run_windows_stability_soak.py --duration-minutes 0.01 --max-cycles 1
python tools\run_windows_stability_soak.py --duration-minutes 45 --summary-path windows-soak-summary.json
python tools\analyze_windows_soak_summary.py windows-soak-summary.json --minimum-duration-minutes 45 --output windows-soak-analysis.json
```

장애 재현 시험은 업로드·CSV 기록 중 프로세스 강제 종료, HTTP/TCP 측정 JSON 확정 직후와 `full` 측정 첫 CSV 행 확정 직후 강제 종료, 디스크 부족, 같은 데이터 폴더 중복 실행, 느린 HTTP 연결, 장시간 TCP 세션 종료 후 재시작을 임시 폴더와 로컬 루프백으로 검증합니다. 실제 사내 자료나 외부 네트워크는 사용하지 않습니다.

GitHub Actions의 `Windows Stability Soak`은 매주 월요일 새벽(KST)에 45분 동안 실제 업로드, 서버 강제 종료·재시작, 기존 파일 다운로드와 TCP 자체 점검을 반복합니다. 각 서버 프로세스의 working set, handle, thread, IPv4/IPv6 TCP socket을 Windows 표준 API로 1초마다 수집하고 시작·종료·최대·증가량을 JSON 요약에 남깁니다. 분석 단계는 실행시간, 완료 횟수, 업로드 바이트, TCP 점검 횟수, PID·표본·지표 구조, 전체 90%와 마지막 분석 구간 100% 계측 가용성을 먼저 검증합니다. 이후 같은 단계의 새 프로세스 기준값 증가, 높은 상태 고정, 강한 peak와 PID 내부 증가를 검사하며 `PASS_NO_REPEATED_PROCESS_GROWTH`, `REVIEW_RESOURCE_ANOMALY`, `INCONCLUSIVE_TELEMETRY`, `FUNCTIONAL_FAIL`로 구분합니다. PASS는 반복 재기동 추세에 관한 판정이며 한 프로세스의 장기 누수 부재를 증명하지 않습니다. API를 사용할 수 없는 항목은 추정값으로 채우지 않고 `partial` 또는 `unavailable`과 원인을 기록한 뒤 나머지 시험을 계속합니다. 필요할 때는 Actions 화면에서 30분, 45분 또는 60분으로 수동 실행할 수 있습니다.

전체 소스 검사:

```bat
.venv\Scripts\python -m compileall app_version.py app.py bounded_server.py probe_client.py startup_ports.py runtime_stability.py upload_transactions.py measurement_transactions.py network_sustained.py sustained_excel.py excel_report.py network_measurement.py result_storage.py network_probe tests tools
node --check static\network_check.js
node --check static\network_sustained.js
node --check static\network_probe.js
node --check static\throughput_chart.js
node --check static\operations_dashboard.js
```

실행파일 자체 점검:

```bat
InternalUploadServer.exe --smoke-check
InternalUploadServer.exe --probe-self-check
client-template\NetworkProbeClient.exe --self-check
```

UDP 손실·지터 측정과 Android 네이티브 TCP 클라이언트는 현재 포함되지 않습니다.
