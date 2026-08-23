# 검증 보고서

## 검증 원칙

성공한 마지막 명령만 보는 workflow는 앞선 실패를 숨길 수 있습니다. 모든 PowerShell native 명령 직후 `$LASTEXITCODE`를 확인하고 실패 시 즉시 `throw`합니다. 이 계약 자체도 회귀 테스트로 검사합니다.

## 자동 검증 구성

| 게이트 | 환경 | 주요 증거 |
|---|---|---|
| PR Validation | GitHub-hosted Windows | compileall, JavaScript syntax, pytest, secret scan, fault suite, pip check, v0.6.0 ZIP build/verifier |
| main push | GitHub-hosted Windows | PR Validation과 같은 source/package gate |
| CodeQL default setup | GitHub-hosted Linux | Python/JavaScript security query |
| Security Scan | GitHub-hosted Linux | tracked secret scan |
| Stability Soak | GitHub-hosted Windows | 합성 업로드, 루프백 TCP self-check, 서버 재시작, process 자원 표본, 분석 후처리 |
| Release | annotated tag의 GitHub-hosted Windows | tag/commit 일치, clean build, server/client self-check, ZIP/SHA/SBOM 게시 |

## P0 회귀 시나리오

- pytest가 실패하면 뒤 fault suite·pip check가 실행돼도 workflow가 성공할 수 없음
- Windows CP1252 환경과 무관하게 soak/analyzer JSON stdout을 UTF-8 bytes로 기록
- Step Summary에는 최대 16KiB의 Markdown 핵심 결과만 기록
- 원시 summary·analysis JSON과 Markdown은 artifact에 보존
- 기능 soak 실패와 분석 후처리 실패를 서로 다른 단계로 표시
- 비루프백 HTTP는 unauthenticated 요청 거부
- cookie state-changing 요청은 CSRF 없으면 거부
- TCP unsigned/tampered/expired/replayed frame은 상태 변경 전에 거부
- enrollment token은 만료·재사용 시 거부

## soak 판정

soak는 합성 파일과 루프백 TCP로 서버 기동/종료, 업로드 transaction, TCP self-check, 재시작 후 저장 상태, working set·handle·thread·TCP socket 표본과 독립 분석 JSON 생성을 반복 확인합니다.

분석 결과는 `PASS_NO_REPEATED_PROCESS_GROWTH`, `REVIEW_RESOURCE_ANOMALY`, `INCONCLUSIVE_TELEMETRY`, `FUNCTIONAL_FAIL`로 구분합니다. PASS는 해당 runner·시간·합성 시나리오에서 반복 프로세스 증가 조건을 찾지 못했다는 뜻일 뿐, 장기 누수 부재나 현장 성능을 증명하지 않습니다.

## Release 독립 검증

`tools/verify_release_zip.py`는 ZIP 내부에서 다음을 다시 계산합니다.

- 필수/금지 파일과 경로
- server/client onedir 분리
- config schema와 probe 기본 설정
- header-only 운영 CSV
- security manifest의 source commit, file size와 SHA-256
- SBOM dependency와 lock hash
- 내부 checksum
- launcher UTF-8 no-BOM

게시 후에는 GitHub Release에서 ZIP·`.sha256`·독립 SBOM을 다시 내려받아 asset 이름, checksum과 ZIP verifier를 별도로 확인합니다.

## 검증하지 않은 것

- 실제 조직 네트워크의 처리량, 지연, 손실 또는 SLA
- 특정 방화벽, EDR, proxy, 브라우저와의 현장 호환성
- 인터넷 공개 서비스 수준의 TLS termination·WAF·다중 사용자 권한
- Windows EXE publisher 신원과 코드 서명 chain
- 압축파일 내부 malware
- 45분을 넘는 연속 운영 전체와 모든 자원 누수

따라서 저장소의 수치와 자료는 합성 CI 검증으로만 표현하며 현장 성과로 인용하지 않습니다.
