# 파일 전송·네트워크 진단 보안 모델

## 신뢰 경계

이 프로젝트는 **신뢰된 내부망**을 전제로 합니다. 인터넷 공개형 파일 공유 서비스나 다중 사용자 인증 플랫폼이 아닙니다.

```text
허가된 내부 사용자
        ↓ HTTP
Internal Upload Server
        ↓
STORAGE_ROOT / measurement result
```

사용자 인증 계층이 없는 현재 구조에서는 네트워크 접근 제어와 운영 환경 분리가 중요한 전제입니다.

## 업로드 경로 제한

사용자가 지정하는 하위 경로는 설정된 `STORAGE_ROOT`를 벗어날 수 없습니다.

차단 대상:

- 절대 경로
- `..` 경로 순회
- storage root 밖으로 해석되는 경로

파일 시스템 경계 검증 없이 사용자 입력 경로를 직접 사용하지 않습니다.

## 위험 파일 차단

운영 자료 전달 도구가 원격 실행 통로가 되지 않도록 다음 범주를 차단합니다.

- 실행파일
- script / shortcut
- driver
- installer package
- macro-enabled Office document
- disk image

Windows PE는 확장자만 신뢰하지 않고 `MZ` header도 검사하여 단순 확장자 변경 우회를 줄입니다.

이 기능은 malware scanner를 대체하지 않습니다. 허용된 ZIP 등 압축파일 내부 콘텐츠는 검사하지 않습니다.

## 다운로드 경계

다운로드는 브라우저가 콘텐츠를 직접 실행/해석할 가능성을 낮추기 위해 attachment와 `application/octet-stream`, `nosniff` 정책을 사용합니다.

## 삭제 권한

파일 삭제 요청은 `DELETE_ALLOWED_IPS`의 개별 IP 정책을 따릅니다. CIDR 기반 광범위 허용이 아니라 명시된 endpoint만 허용하는 현재 계약을 유지합니다.

## 디스크 고갈 방지

고정 파일 크기 제한 대신 운영 디스크의 남은 용량을 보호합니다.

- 최소 1GB reserve
- 동시에 진행 중인 업로드의 예상 미기록 byte도 예약
- 업로드 중 주기적 free-space 재검사
- 공간 부족 시 temp 파일 정리
- 합산 예약 후 부족하면 HTTP 507

## 동시성 경계

- upload worker 최대 4
- web request worker 최대 32
- sustained/TCP 고부하 측정은 shared single-flight 경계

상한을 넘는 요청을 무제한 queue/thread로 받아들이지 않습니다.

## 방화벽·권한

프로그램은 Windows 방화벽을 자동 변경하거나 관리자 권한을 요청하지 않습니다. 필요한 포트 정책은 조직의 관리 절차에서 별도로 적용합니다.

## 데이터와 로그

실제 업로드 파일, 메모, 운영 CSV/JSON 결과, IP는 운영 데이터입니다. 공개 저장소에는 초기 CSV header와 문서용 fixture만 유지합니다.

오류 응답과 진단에서는 raw local path, traceback, 다른 사용자의 측정 결과를 불필요하게 노출하지 않는 것을 원칙으로 합니다.

## Release 검증 자료

Windows Release에는 다음 자료가 포함됩니다.

- `SECURITY_REVIEW_KO.md`
- `security_manifest.json`
- `sbom.cdx.json`
- `SHA256SUMS.txt`

`security_manifest.json`은 패키지 파일의 크기·SHA-256과 source commit을 기록합니다. 현재 binary는 코드 서명되지 않았으므로 이 자료는 **출처·무결성 검토 수단**이지 서명된 publisher 신원을 의미하지 않습니다.

## 잔여 위험

현재 구조에서 의도적으로 남아 있는 제한입니다.

- 애플리케이션 사용자 인증 없음
- unsigned Windows binaries
- 고정 upload size limit 없음
- archive 내부 malware inspection 없음
- trusted internal network 전제
- TCP 측정 client의 장시간 polling/connection 가능

따라서 외부 인터넷이나 불특정 사용자 네트워크에 직접 노출하지 않습니다.
