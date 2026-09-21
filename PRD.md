# 토스증권 주식 자동매매 시스템 PRD

## 1. 목표

Python과 FastAPI로 국내·미국 주식 자동매매 시스템을 구축한다. 사용자가 설정한 전략으로 시세를 분석하고, 위험 한도 안에서 주문과 체결을 관리한다.

초기에는 가상금액으로 검증하고, 안정화 후 소액 실거래를 거쳐 실제 계좌 운용으로 전환한다.

## 2. 운영 모드

- `PAPER`: 가상 현금·포트폴리오·체결을 사용하며 실제 주문 API를 호출하지 않는다.
- `DRY_RUN`: 실제 시세와 계좌를 조회하지만 주문은 기록만 한다.
- `LIVE`: 토스증권 주문 API로 실제 거래한다.

기본 모드는 `PAPER`이며, `LIVE` 전환은 자동화하지 않는다.

## 3. 토스증권 연동

- 인증: OAuth 2.0 Client Credentials, `POST /oauth2/token`
- REST: `https://openapi.tossinvest.com`
- WebSocket: `wss://openapi-ws.tossinvest.com/ws/v1`
- 시세: 현재가, 호가, 체결, 캔들, 종목·장 운영 정보
- 계좌: 계좌, 보유 자산, 매수 가능 금액, 매도 가능 수량
- 주문: 생성, 정정, 취소, 조회, 조건주문
- 계좌·주문 요청에는 `X-Tossinvest-Account` 헤더 사용
- 주문에는 `clientOrderId`를 사용해 중복 주문 방지
- Rate Limit, 401·403·422·429 오류, 허용 IP 제한 처리

실시간 시세는 WebSocket으로 수신하고, 연결 복구 후 REST 주문 조회로 상태를 재동기화한다.

## 4. 핵심 기능

1. API 자격 정보와 계좌 연결
2. REST·WebSocket 시세 수집
3. 전략별 진입·청산 신호 생성
4. 종목별·전체 투자금, 주문 금액, 일일 손실·주문 횟수 제한
5. 주문 상태·부분 체결·체결 결과 관리
6. 전략 시작·중지와 긴급 주문 차단
7. 거래·오류·전략 이벤트 감사 로그
8. 가상 포트폴리오와 손익 계산

## 5. 기술 구조

- Python 3.12+, FastAPI, Uvicorn
- `httpx`, `websockets`, Pydantic v2
- SQLAlchemy 2.x, Alembic, PostgreSQL, Redis
- pytest, pytest-asyncio, Ruff, mypy

```text
FastAPI API
  ├─ Toss REST/WebSocket Client
  ├─ Strategy Engine
  ├─ Risk Manager
  ├─ Order Manager
  └─ PostgreSQL / Redis
```

라우터, 토스 클라이언트, 전략, 위험 관리, 주문 처리를 분리한다. 장시간 작업은 비동기 태스크로 실행하고, 다중 워커에서는 Redis 락으로 중복 실행을 막는다.

## 6. 내부 API

- `GET /health`
- `GET /api/v1/market/prices`
- `GET /api/v1/market/candles`
- `GET /api/v1/accounts`
- `GET /api/v1/portfolio`
- `GET /api/v1/orders`
- `POST /api/v1/orders` (기본 `PAPER` 또는 `DRY_RUN`)
- `POST /api/v1/strategies/{id}/start`
- `POST /api/v1/strategies/{id}/stop`
- `POST /api/v1/emergency-stop`

## 7. 데이터와 보안

계좌, 종목, 캔들, 전략, 신호, 주문, 체결, 포지션, 감사 로그를 저장한다. 금액과 수량은 `Decimal`로 처리하고 API 키·토큰은 환경변수 또는 비밀 저장소에서 관리한다.

## 8. 가상매매 안정화 기준

`LIVE` 전환 전에 최소 20거래일 `PAPER` 또는 `DRY_RUN` 운영, 로그 누락 없음, WebSocket 재연결 및 REST 재동기화 성공, 장애·Rate Limit·주문 거부·중복 요청의 안전 처리, 위험 한도와 긴급 중지 검증을 완료한다.

전환 후에는 단일 종목과 소액으로 시작한다. 잔고 불일치, 반복 오류, 손실 한도 초과, 비정상 주문이 발생하면 신규 주문을 차단한다.

## 9. 개발 단계

1. 토큰·계좌·시세 조회
2. WebSocket과 가상 포트폴리오
3. 전략·위험 관리·`PAPER` 실행
4. `DRY_RUN`과 주문 상태 복구
5. 안정화 검증 후 제한적 `LIVE`

## 10. 참고 문서

- [토스증권 Open API](https://developers.tossinvest.com/docs)
- [OpenAPI 명세](https://openapi.tossinvest.com/openapi-docs/latest/openapi.json)
- [FAQ](https://openapi.tossinvest.com/openapi-docs/faq.md)
