# 검증 보고서

이 문서는 Internal Network Transfer & Diagnostics의 자동 검증 범위와 해석 한계를 구분합니다.

## 기본 회귀 검증

Release와 PR 검증은 다음 소스 계약을 확인합니다.

```text
Python compileall
      ↓
JavaScript syntax check
      ↓
pytest regression suite
      ↓
stability fault suite
      ↓
pip dependency check
```

### Python 영역

- 설정값 검증과 잘못된 설정의 fail-closed 처리
- storage root 경로 제한
- upload/download/delete 흐름
- upload transaction recovery
- measurement transaction recovery
- JSON/CSV 결과 정합성
- HTTP/TCP single-flight
- TCP protocol/result handling
- bounded server와 shutdown 상태
- release/security artifact helper

### JavaScript 영역

- HTTP network check
- sustained measurement
- TCP probe
- throughput chart
- operations summary

각 핵심 JS 파일은 최소 syntax check를 통과해야 합니다.

## Fault suite

`tools/run_stability_fault_suite.py`는 단순 정상 흐름과 별도로 저장·복구 오류 상황을 실행합니다. 목적은 중간 실패 후 모호한 상태를 정상 완료로 승격하지 않는지 확인하는 것입니다.

## Windows Stability Soak

`.github/workflows/stability-windows.yml`은 실제 Windows runner에서 upload/TCP/restart 반복 동작을 수행합니다.

기본 scheduled soak:

```text
baseline pytest
    ↓
45-minute upload / TCP / restart soak
    ↓
windows-soak-summary.json
    ↓
resource trend analyzer
    ↓
windows-soak-analysis.json
```

수동 실행 시 30/45/60분을 선택할 수 있습니다.

분석 대상에는 반복 작업 성공 여부뿐 아니라 장시간 실행 중 자원 추세와 restart recovery 상태가 포함됩니다.

## Windows Release 검증

Release build는 `requirements-windows.lock`을 `--require-hashes`로 설치하고 source version과 요청 release version이 일치하는지 확인합니다.

패키지 생성 후 `tools/verify_release_zip.py`는 다음 계약을 검사합니다.

- 필수 server/client executable 존재
- 예상하지 않은 executable·source/test/tool 파일 제외
- ZIP path traversal·중복 Windows path·symlink/encrypted entry 거부
- 운영 결과 JSON 미포함
- CSV는 초기 header만 포함
- default config 계약
- launcher가 PowerShell/elevation을 우회적으로 호출하지 않음
- server/client onedir runtime 분리
- `security_manifest.json` 파일 목록·size·SHA-256 일치
- CycloneDX SBOM 필수 dependency 확인
- `SHA256SUMS.txt` 전체 패키지 파일 hash 일치

## 검증이 증명하지 않는 것

CI가 green이어도 다음을 보장하지 않습니다.

- 특정 사내 스위치·AP·WAN 구간의 품질
- 특정 endpoint의 EDR/방화벽/GPO 영향이 없다는 사실
- 모든 브라우저 버전의 동일한 throughput
- TCP/HTTP 차이의 단일 원인
- 허용된 archive 내부 콘텐츠의 안전성
- 인증이 없는 현재 서비스가 인터넷 노출에 안전하다는 의미
- 모든 용량의 대형 파일을 모든 디스크 조건에서 수용할 수 있다는 의미

## 결과 해석 원칙

측정값은 절대적인 회선 인증 결과가 아니라 **해당 시점의 endpoint-to-endpoint 관측값**입니다. HTTP/TCP 결과, endpoint 자원, 네트워크 경로, 보안 제품 로그를 함께 봐야 합니다.

## 실제 운영 데이터

공개 검증 자료에는 실제 사내 IP, hostname, 업로드 파일, 메모, 운영 CSV/JSON 원문을 포함하지 않습니다. 실제 환경 검증 결과를 공개할 경우 수치와 조건만 비식별화해 기록해야 합니다.
