# Security Policy

## 민감정보

공개 Issue, Pull Request, screenshot, fixture, 로그에 다음 정보를 포함하지 마십시오.

- 실제 내부 IP / hostname
- 업로드된 실제 파일이나 메모
- 채워진 운영 CSV/JSON 결과
- 사내 경로·조직명·사용자 식별 정보
- 방화벽·보안 정책의 비공개 구성 원문

재현은 RFC 5737 문서용 IP와 합성 파일명/측정값을 사용하십시오.

## 안전 경계

이 프로젝트는 신뢰된 내부망 사용을 전제로 하며 인터넷 공개 서비스를 목표로 하지 않습니다.

다음 변경은 보안·운영 회귀로 간주합니다.

- storage root 밖으로 쓰기 허용
- 업로드 파일 실행 기능 추가
- 위험 파일/MZ header 차단 우회
- 방화벽 자동 변경 또는 자동 권한 상승 추가
- 삭제 허용 IP 검사를 우회
- 측정 single-flight 제거
- transaction/recovery 오류를 정상 완료로 조용히 승격
- release security manifest/SHA/SBOM verifier 약화

## 보고

민감한 취약점의 실제 운영 데이터를 공개 이슈에 첨부하지 마십시오. 이미 비밀 또는 운영 파일이 공개된 경우 해당 정보의 폐기/교체 가능성을 먼저 검토하고 Git history와 Release artifact의 복사본도 별도로 확인해야 합니다.
