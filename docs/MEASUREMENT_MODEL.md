# HTTP / TCP 측정 모델

## 왜 두 측정 방식을 분리하는가

HTTP 처리량은 브라우저, HTTP framing, 애플리케이션 route와 서버 구현의 영향을 함께 받습니다. TCP probe는 이 애플리케이션 계층과 분리된 전송 경로를 제공합니다.

따라서 두 결과는 서로 대체 관계가 아니라 **문제 범위를 좁히기 위한 비교 관측값**입니다.

```text
HTTP 느림 + TCP 정상
→ 브라우저/HTTP/애플리케이션 계층 영향 가능성

HTTP 느림 + TCP 느림
→ 공통 네트워크 경로 또는 endpoint 자원 영향 확인 필요
```

자동화가 이 결과만으로 장애 원인을 확정하지는 않습니다.

## HTTP 데이터량 기준

선택한 데이터량의 전송이 끝날 때까지 측정합니다.

- 10MB
- 50MB
- 100MB
- 500MB
- 1024MB

관측 항목:

- 전송 byte
- 경과 시간
- 평균 Mbps / MB/s
- 진행 중 최근 속도
- 현재 속도 기준 예상 전송 시간

대용량 1024MB 측정은 사용자에게 부하 확인을 먼저 요구합니다.

## HTTP 시간 기준

시간 기반 측정은 워밍업과 본 측정을 분리합니다.

```text
준비
 ↓
3초 warm-up
 ↓
10초 또는 30초 measurement
 ↓
1초 sample 집계
 ↓
평균 / 최저 / 최고 / 변동률
```

전체 측정에서는 upload와 download가 독립 단계입니다. 한 방향이 완료된 후 다른 방향이 실패하면 완료 결과를 폐기하지 않고 `부분 완료`로 보존합니다.

## Single-flight

서버 전체에서 고부하 측정을 동시에 여러 개 실행하면 서로의 throughput을 깎아 결과 의미가 약해집니다. `network_measurement.py`의 공통 gate를 통해 한 시점의 측정 소유권을 제한합니다.

경쟁 요청은 기존 측정과 섞어 실행하지 않고 명시적인 busy 상태로 처리합니다.

## TCP probe

TCP 측정은 별도 port와 전용 client/server 흐름을 사용합니다.

```mermaid
sequenceDiagram
    participant C as Probe Client
    participant S as Probe Server
    participant R as Result Store

    C->>S: session / measurement request
    S-->>C: protocol negotiation
    C<->>S: bounded TCP transfer
    S->>R: result intent + JSON
    S->>R: CSV reconciliation
    S-->>C: completion state
```

TCP probe가 사용할 수 없더라도 웹 파일 전달과 HTTP 측정 전체를 장애로 취급하지 않습니다.

## 결과의 현재성

측정 결과는 session identity와 client ownership을 기준으로 읽습니다. 다른 client의 sustained result를 임의로 조회하는 흐름을 허용하지 않으며, 결과 파일 read/JSON validation 실패는 raw 경로나 traceback 대신 정규화된 오류로 변환합니다.

## 결과 저장

측정 종료 상태는 JSON을 기준으로 보존하고 CSV는 조회·이력용 인덱스 역할을 합니다. 중간 종료로 JSON과 CSV가 어긋난 경우 transaction marker와 startup recovery가 이를 재조정합니다.

## 취소와 실패

- 취소된 세션은 완료처럼 표시하지 않습니다.
- 중단 시점까지의 진행률을 보존합니다.
- 완료된 반대 방향이 있으면 partial result로 유지할 수 있습니다.
- 결과 저장 실패는 조용히 정상 성공으로 승격하지 않습니다.

## 해석 시 주의

이 측정은 다음을 단독으로 증명하지 않습니다.

- ISP/WAN 전체 품질
- 특정 스위치 포트나 케이블이 장애 원인이라는 사실
- HTTP와 TCP 차이의 단일 원인
- 무선 구간의 RF 원인

결과는 endpoint CPU/디스크, 브라우저, 경로 혼잡, 보안 제품 등 다른 관측값과 함께 해석해야 합니다.
