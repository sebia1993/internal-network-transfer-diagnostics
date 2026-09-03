# 보안 모델

## 범위와 신뢰 경계

이 프로젝트는 신뢰할 수 있는 내부망에서 사용하는 운영 보조 도구입니다. 인터넷 공개 서비스나 다중 사용자 권한 플랫폼은 목표가 아닙니다. 웹 로그인 인증은 사용하지 않으며, 네트워크 경계 자체가 웹 접근 통제의 1차 수단입니다.

| 경계 | 적용 통제 | 남는 위험 |
|---|---|---|
| 브라우저 → 서버 HTTP | 직접 접근, unsafe 요청 CSRF, 보안 header | 서버 포트에 도달 가능한 사용자는 웹 기능을 사용할 수 있고 내장 HTTP는 평문임 |
| Windows client → 등록 API | 30~3600초 범위의 1회용 enrollment Bearer | 최초 사용 전 탈취되면 선점 가능 |
| TCP control | HMAC-SHA256, timestamp, 128-bit 이상 nonce, replay cache | payload 자체는 암호화되지 않음 |
| 업로드 입력 → 파일 시스템 | storage root 고정, 경로 순회 차단, 위험 확장자·MZ 검사 | 허용된 압축파일 내부는 검사하지 않음 |
| 실행 패키지 → 사용자 | hash-pinned lock, SHA-256, CycloneDX SBOM, manifest, CodeQL | EXE 코드 서명 없음 |

## HTTP 접근 제어와 CSRF

- 웹 로그인용 access token과 master Bearer 인증은 사용하지 않습니다.
- `127.0.0.0/8`, `::1`, 비루프백 주소 모두 별도 로그인 없이 화면과 GET API에 접근할 수 있습니다.
- 비루프백 브라우저의 POST/PUT/PATCH/DELETE는 `X-CSRF-Token` 또는 `_csrf_token`을 검증합니다.
- CSRF 값은 인증 자격증명이 아니라 cross-origin 상태 변경 요청을 줄이기 위한 값입니다.
- TCP 클라이언트 등록/제어 API는 별도 enrollment/agent 인증 흐름을 사용하므로 웹 CSRF 처리와 분리합니다.
- 루프백 요청은 기존 자체 점검과 로컬 운영 호환성을 위해 CSRF 검증을 우회합니다.

기본 `HOST=0.0.0.0` 바인딩은 한 내부망 서버가 여러 네트워크 인터페이스에서 요청을 받기 위한 의도된 동작입니다. 다만 애플리케이션 자체 웹 인증이 없으므로 `0.0.0.0`은 접근 통제가 아닙니다. Windows 방화벽, VLAN/ACL, VPN 또는 서버가 연결된 신뢰 구간에서 접근 가능한 주소와 대역을 제한해야 합니다. 단일 인터페이스만 필요하면 `HOST`를 해당 내부 주소로 좁히세요.

역방향 프록시를 사용할 때 애플리케이션은 `X-Forwarded-For`를 신뢰하지 않습니다. 실제 peer가 프록시이면 그 peer 주소를 기준으로 판단합니다. 프록시 신뢰 설정과 원본 IP 정책은 운영자가 별도 경계에서 구성해야 합니다.

## 웹 access token 제거

이전 버전의 다음 웹 인증 요소는 더 이상 사용하지 않습니다.

- `INTERNAL_TRANSFER_ACCESS_TOKEN` 환경 변수
- `[security] ACCESS_TOKEN_FILE`
- `[security] SESSION_TTL_MINUTES`
- `data/.internal-transfer-access-token`
- `/login`에서의 토큰 입력
- 웹 API용 master Bearer 인증

기존 `config.ini`에 `ACCESS_TOKEN_FILE`이나 `SESSION_TTL_MINUTES`가 남아 있어도 호환성을 위해 설정 파싱은 가능하지만 인증에는 사용되지 않습니다. 기존 설치에 `data/.internal-transfer-access-token` 파일이 남아 있다면 새 버전에서는 읽지 않으므로 운영 확인 후 삭제할 수 있습니다.

Git 추적 파일은 대표 private key, GitHub token 등 비밀 형식을 별도 스캔합니다. 이 스캔은 모든 비밀 형식을 보장하지 않습니다.

## Windows client 등록

클라이언트 ZIP을 받을 때 서버는 짧은 수명의 enrollment token을 발급하고 digest와 만료 시각만 메모리에 보관합니다.

- 한 번 소비하면 같은 token은 다시 사용할 수 없습니다.
- 만료되거나 이미 사용한 ZIP은 새로 받아야 합니다.
- 등록 성공 후 agent Bearer와 TCP HMAC key는 프로세스 메모리에만 둡니다.
- ZIP의 `client-config.json`은 등록 후 삭제하도록 안내합니다.
- 서버 재시작으로 메모리 상태가 사라지면 새 ZIP을 발급해야 합니다.

이 enrollment token은 웹 로그인 토큰과 별개이며 이번 웹 인증 제거 이후에도 유지됩니다.

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
- 삭제는 `DELETE_ALLOWED_IPS`를 추가 확인
- 업로드/결과는 temp, flush/fsync, marker, atomic replace, startup recovery 순서
- 모호한 transaction은 정상으로 승격하지 않고 새 작업을 차단

`DELETE_ALLOWED_IPS`는 일반 웹 접근 인증을 대신하지 않으며 삭제 작업에만 적용되는 추가 제한입니다.

## HTTP 보안 header와 session cookie

응답에는 `no-store`, `nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`와 same-origin CSP를 적용합니다.

CSRF 값을 유지하기 위해 Flask session cookie를 사용하지만 이 cookie는 로그인 상태나 사용자 권한을 나타내지 않습니다. `HttpOnly`, `SameSite=Strict`를 적용합니다. 내장 HTTP 때문에 `Secure` cookie는 기본으로 켤 수 없습니다. TLS proxy 환경에서는 프록시와 배포 정책에서 HTTPS 강제와 secure cookie 전략을 함께 검토해야 합니다.

## 배포 무결성

Release ZIP에는 다음 자료를 포함합니다.

- `security_manifest.json`: source commit과 파일 size/SHA-256
- `SHA256SUMS.txt`: 패키지 내부 파일 checksum
- `sbom.cdx.json`: hash-pinned Windows dependency의 CycloneDX SBOM
- `SECURITY_REVIEW_KO.md`: 예상 동작과 남는 위험

Release에는 ZIP, ZIP SHA-256, 독립 SBOM을 별도 asset으로 게시합니다. 이는 코드 서명을 대신하지 않습니다.

## 명시적 한계와 운영 조치

- 서버 웹 포트에 도달 가능한 사용자는 웹 UI와 웹 API를 사용할 수 있으므로 신뢰할 수 있는 내부망·VPN과 네트워크 ACL을 사용합니다.
- 인터넷 또는 불특정 사용자 네트워크에 직접 노출하지 않습니다.
- 신뢰 경계를 넘으면 TLS proxy를 적용합니다.
- 다운로드한 TCP client ZIP과 압축 해제 설정 파일의 접근 권한을 제한합니다.
- TCP client 등록 정보가 노출되었다고 의심되면 서버를 재시작하고 기존 ZIP을 폐기한 뒤 새 ZIP을 발급합니다.
- 이전 버전의 `.internal-transfer-access-token` 파일은 새 버전에서 사용하지 않으므로 업그레이드 확인 후 제거할 수 있습니다.
- 업로드 허용 파일도 조직의 malware scanning 정책을 적용합니다.
- 실제 IP, hostname, 결과, 업로드 파일은 공개 Issue·PR·screenshot에 넣지 않습니다.
