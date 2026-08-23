# 보안 모델

## 범위와 신뢰 경계

이 프로젝트는 인증된 운영자가 사용하는 내부망 보조 도구입니다. 인터넷 공개 서비스나 다중 사용자 권한 플랫폼은 목표가 아닙니다.

| 경계 | 적용 통제 | 남는 위험 |
|---|---|---|
| 브라우저 → 서버 HTTP | 비루프백 로그인 또는 master Bearer, cookie 요청 CSRF | 내장 HTTP는 평문이므로 도청 가능 |
| Windows client → 등록 API | 30~3600초 범위의 1회용 enrollment Bearer | 최초 사용 전 탈취되면 선점 가능 |
| TCP control | HMAC-SHA256, timestamp, 128-bit 이상 nonce, replay cache | payload 자체는 암호화되지 않음 |
| 업로드 입력 → 파일 시스템 | storage root 고정, 경로 순회 차단, 위험 확장자·MZ 검사 | 허용된 압축파일 내부는 검사하지 않음 |
| 실행 패키지 → 사용자 | hash-pinned lock, SHA-256, CycloneDX SBOM, manifest, CodeQL | EXE 코드 서명 없음 |

## HTTP 인증

- `127.0.0.0/8`과 `::1` 요청은 로컬 관리 편의를 위해 인증 없이 허용합니다.
- 그 외 주소는 토큰 로그인 또는 `Authorization: Bearer ...`가 필요합니다.
- 로그인 session은 `HttpOnly`, `SameSite=Strict` cookie를 사용합니다.
- cookie 기반 POST/PUT/PATCH/DELETE는 `X-CSRF-Token` 또는 `_csrf_token`을 검증합니다.
- master Bearer는 cookie를 사용하지 않으므로 CSRF 검증 대상이 아닙니다.
- 세션은 설정된 TTL을 넘으면 실패 시 차단합니다.

역방향 프록시를 사용할 때 애플리케이션은 `X-Forwarded-For`를 신뢰하지 않습니다. 실제 peer가 프록시이면 그 peer 주소를 기준으로 판단합니다. 프록시 신뢰 설정과 원본 IP 정책은 운영자가 별도 경계에서 구성해야 합니다.

## 비밀 생성·보관

접근 토큰 검색 순서:

1. `INTERNAL_TRANSFER_ACCESS_TOKEN` 환경 변수
2. `[security] ACCESS_TOKEN_FILE`
3. 파일이 없으면 cryptographic random token 생성

토큰 값은 서버 시작 메시지, URL, CLI 인수, diagnostic log에 기록하지 않습니다. 파일 경로만 안내합니다. POSIX 기존 파일에 group/other 권한이 있거나 symlink이면 시작을 거부합니다. Windows에서는 서버 전용 계정과 NTFS ACL로 파일을 보호해야 합니다.

Git 추적 파일은 대표 private key, GitHub token, 앱 접근 토큰 패턴을 별도 스캔합니다. 이 스캔은 모든 비밀 형식을 보장하지 않습니다.

## Windows client 등록

클라이언트 ZIP을 받을 때 서버는 짧은 수명의 enrollment token을 발급하고 digest와 만료 시각만 메모리에 보관합니다.

- 한 번 소비하면 같은 token은 다시 사용할 수 없습니다.
- 만료되거나 이미 사용한 ZIP은 새로 받아야 합니다.
- 등록 성공 후 agent Bearer와 TCP HMAC key는 프로세스 메모리에만 둡니다.
- ZIP의 `client-config.json`은 등록 후 삭제하도록 안내합니다.
- 서버 재시작으로 메모리 상태가 사라지면 새 ZIP을 발급해야 합니다.

## TCP HMAC과 재전송 방지

프로토콜 `v3` 제어 frame은 payload의 canonical JSON과 다음 필드를 HMAC-SHA256으로 서명합니다.

- 알고리즘 식별자
- Unix timestamp
- cryptographic nonce
- signature

서버와 client는 signature를 먼저 검증한 뒤 timestamp 범위와 nonce 재사용을 확인합니다. unsigned, 잘못된 signature, 만료 timestamp, 중복 nonce는 상태 변경 전에 거부합니다. 루프백에서는 자체 점검 호환을 위해 legacy frame을 허용하지만, 비루프백에서는 HMAC이 필수입니다.

시스템 시계가 허용 범위를 크게 벗어나면 정상 요청도 거부됩니다. 서버와 client의 시간 동기화를 유지하세요.

## 파일·저장 경계

- 절대 경로, `..`, storage root 밖으로 해석되는 경로 차단
- 실행파일·스크립트·설치 패키지·매크로 문서·디스크 이미지 차단
- 확장자와 별개로 Windows PE `MZ` header 검사
- 다운로드는 attachment, octet-stream, `nosniff`
- 삭제는 인증 후에도 `DELETE_ALLOWED_IPS`를 추가 확인
- 업로드/결과는 temp, flush/fsync, marker, atomic replace, startup recovery 순서
- 모호한 transaction은 정상으로 승격하지 않고 새 작업을 차단

## HTTP 보안 header

응답에는 `no-store`, `nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`와 same-origin CSP를 적용합니다. 내장 HTTP 때문에 `Secure` cookie는 기본으로 켤 수 없습니다. TLS proxy 환경에서는 프록시와 배포 정책에서 HTTPS 강제와 secure cookie 전략을 함께 검토해야 합니다.

## 배포 무결성

Release ZIP에는 다음 자료를 포함합니다.

- `security_manifest.json`: source commit과 파일 size/SHA-256
- `SHA256SUMS.txt`: 패키지 내부 파일 checksum
- `sbom.cdx.json`: hash-pinned Windows dependency의 CycloneDX SBOM
- `SECURITY_REVIEW_KO.md`: 예상 동작과 남는 위험

Release에는 ZIP, ZIP SHA-256, 독립 SBOM을 별도 asset으로 게시합니다. 이는 코드 서명을 대신하지 않습니다.

## 명시적 한계와 운영 조치

- 신뢰할 수 있는 내부망·VPN을 사용하고 신뢰 경계를 넘으면 TLS proxy를 적용합니다.
- 토큰 파일, 다운로드한 client ZIP과 압축 해제 설정 파일의 접근 권한을 제한합니다.
- 외부 노출이 의심되면 토큰 파일을 안전하게 교체하고 서버를 재시작하며 발급한 client ZIP을 폐기합니다.
- 업로드 허용 파일도 조직의 malware scanning 정책을 적용합니다.
- 실제 IP, hostname, 결과, 업로드 파일은 공개 Issue·PR·screenshot에 넣지 않습니다.
