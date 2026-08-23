# Security Policy

## 지원 버전

보안 수정은 최신 정식 릴리스와 `main`에 적용합니다. 과거 release asset과 tag는 이력 보존을 위해 이동하거나 덮어쓰지 않습니다.

## 공개하면 안 되는 정보

Issue, Pull Request, screenshot, fixture와 로그에 다음을 넣지 마세요.

- 접근·enrollment·agent·session token 또는 token file 내용
- 실제 내부 IP, hostname, 사용자·조직 식별 정보
- 업로드된 실제 파일, 메모와 사내 경로
- 채워진 운영 CSV/JSON/Excel과 diagnostic log
- 방화벽·EDR·proxy 정책의 비공개 원문

재현은 RFC 5737 문서용 주소와 합성 파일·결과를 사용하세요.

## 취약점 보고

실제 비밀이나 운영 자료가 포함된 취약점은 공개 Issue에 올리지 마세요. GitHub 저장소의 비공개 보안 보고 기능을 사용하고, 기능을 사용할 수 없다면 민감한 재현 자료 없이 최소 설명만 남겨 안전한 전달 방법을 먼저 합의하세요.

보고에는 영향받는 버전·commit, 영향 경계, 합성 최소 재현, 기대/실제 동작과 노출 여부를 포함하세요.

## 즉시 대응

비밀이나 운영 파일이 공개됐다면 Git 이력 수정만으로 끝난 것으로 간주하지 마세요.

1. 해당 token/정보를 폐기·교체합니다.
2. 서버를 재시작해 메모리 token/session을 무효화합니다.
3. Git history, fork, Actions artifact와 Release asset 복사본을 각각 확인합니다.
4. 실제 운영 파일이면 조직의 incident 절차를 따릅니다.

## 보안 회귀로 간주하는 변경

- 비루프백 인증·CSRF 우회 또는 fail-open
- TCP HMAC, timestamp, nonce replay 방지 약화
- 장기 비밀을 config, ZIP, URL, CLI 또는 log에 기록
- storage root 밖 쓰기·다운로드·삭제 허용
- 위험 파일/MZ 차단 우회 또는 업로드 파일 실행
- transaction/recovery 오류를 정상으로 승격
- 방화벽 자동 변경·권한 상승 추가
- hash-pinned lock, CodeQL/secret scan, SHA/SBOM/manifest/ZIP verifier 약화

상세 위협 모델은 [docs/SECURITY_MODEL.md](../docs/SECURITY_MODEL.md)를 참고하세요.
