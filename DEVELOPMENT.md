# 개발 가이드

## 프로젝트 범위

이 저장소는 신뢰된 내부망에서 사용하는 **파일 전달 + HTTP/TCP 네트워크 진단 도구**입니다. 장비 관리, 티켓 시스템, 사용자 권한 플랫폼으로 범위를 확장하지 않습니다.

## 변경 원칙

- 기존 upload/measurement workflow를 작은 단위로 변경합니다.
- 운영 데이터보다 단순 CSV·JSON·파일 시스템 계약을 우선합니다.
- 실제 업로드 파일, 채워진 운영 CSV/JSON, 내부 IP/hostname을 Git에 올리지 않습니다.
- 방화벽 자동 변경, 권한 상승, 업로드 파일 실행 기능을 추가하지 않습니다.
- server와 TCP client entrypoint 분리를 유지합니다.
- measurement single-flight를 우회해 고부하 측정을 동시에 실행하지 않습니다.
- transaction marker와 startup recovery를 무시하고 결과를 직접 정상 상태로 승격하지 않습니다.
- release security artifact와 hash-pinned dependency 계약을 유지합니다.
- 비루프백 HTTP 인증·CSRF와 TCP HMAC·replay 방지를 fail-closed로 유지합니다.
- 접근·enrollment·agent·session token 값을 log, URL, CLI, fixture에 기록하지 않습니다.

## 주요 영역

- `app.py` — Flask route, config, upload/download/delete, network measurement API
- `bounded_server.py` — bounded HTTP workers, inactive connection, shutdown
- `upload_transactions.py` — upload/delete durable transaction 및 recovery
- `measurement_transactions.py` — HTTP/TCP measurement intent와 JSON/CSV reconciliation
- `network_sustained.py` — duration-based HTTP 측정
- `network_measurement.py` — shared single-flight gate
- `network_probe/` — TCP probe protocol/server/client/statistics/result
- `result_storage.py` — temp/fsync/atomic JSON write
- `runtime_stability.py` — CSV tail recovery, instance lock, diagnostics
- `tools/` — Windows release, security artifact, fault suite, soak/analyzer

## 기본 검증

```powershell
python -m compileall access_security.py app_version.py app.py bounded_server.py probe_client.py startup_ports.py runtime_stability.py upload_transactions.py measurement_transactions.py network_sustained.py sustained_excel.py excel_report.py network_measurement.py result_storage.py network_probe tests tools
node --check static/security.js
node --check static/network_check.js
node --check static/network_sustained.js
node --check static/network_probe.js
node --check static/throughput_chart.js
node --check static/operations_dashboard.js
python -m pytest -q
python tools/scan_tracked_secrets.py
python tools/run_stability_fault_suite.py
python -m pip check
```

## Windows 장시간 검증

```powershell
python tools/run_windows_stability_soak.py --duration-minutes 45 --summary-path windows-soak-summary.json
python tools/analyze_windows_soak_summary.py windows-soak-summary.json --minimum-duration-minutes 45 --output windows-soak-analysis.json
```

## Release 패키지 검증

현재 source version과 동일한 release version으로 clean worktree에서 빌드합니다.

```powershell
.\tools\build_windows_release.ps1 -Version v0.6.0
python tools\verify_release_zip.py --zip dist\internal-upload_v0.6.0_windows.zip --version v0.6.0
```

Release build가 생성한 `security_manifest.json`, SBOM, SHA256 자료를 제거하거나 verifier를 우회하지 않습니다.

## 문서 변경

사용자 동작이 바뀌면 README와 관련 운영 문서를 함께 수정합니다. 과거 버전 상세 이력은 `CHANGELOG.md`와 `RELEASE_NOTES.md`에 유지하고 README는 현재 구조·사용법·안전 경계를 중심으로 유지합니다.

## 테스트 데이터

RFC 5737 문서용 주소와 합성 데이터만 사용합니다. 실제 장애 로그, 내부 파일명, 사용자 메모, 운영 IP를 fixture나 screenshot에 복사하지 않습니다.
