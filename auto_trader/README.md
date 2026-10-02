# 자동매매 서버

```powershell
python -m pip install -r requirements.txt
python -m auto_trader
```

브라우저에서 `http://127.0.0.1:8000/docs`를 열면 Swagger UI를 사용할 수 있다.

기본값은 `TRADING_MODE=PAPER`, `LIVE_TRADING_ENABLED=false`다. 서버를 다시 시작해도 LIVE로 자동 진입하지 않으며, 웹 자동매매 버튼은 PAPER 전용이다.

```powershell
Invoke-RestMethod -Method Put 'http://127.0.0.1:8000/api/v1/market/prices/005930?price=70000'
Invoke-RestMethod -Method Post 'http://127.0.0.1:8000/api/v1/orders' -ContentType 'application/json' -Body '{"symbol":"005930","side":"BUY","quantity":"10","price":"70000"}'
Invoke-RestMethod 'http://127.0.0.1:8000/api/v1/portfolio'
```

웹의 **실계좌 확인 · 읽기 전용** 버튼은 토스 계좌의 KRW/USD 매수 가능 금액, 보유 종목, 미체결 주문 수를 PAPER와 별도로 보여준다. 읽기 전용 API는 `/api/v1/live/accounts`, `/api/v1/live/snapshot`, `/api/v1/live/orders/journal`이다. `/api/v1/operations/status`에는 재시작 복구와 운영 경고 상태가 있다.

`POST /api/v1/live/orders/preflight`는 실제 주문을 보내지 않고 장 시간, 최신 시세, 종목 상태, 상·하한가, 호가 단위, 수수료와 실계좌 위험 한도를 확인한다. LIVE 수동 주문은 별도 설정과 명시적 모드 전환·주문 확인·고유 `client_order_id`가 모두 필요하다. 실제 현금 주문·취소·부분 체결의 왕복 검증은 아직 하지 않았다. 자동 LIVE 전략도 제공하지 않는다.

자동매매 상태·이력과 주문 의도·체결 관측 기록은 `auto_trader.db`에 저장한다. 서버 재시작 때 ID가 있는 미완료 주문은 토스 주문 상세로 재동기화한다. 주문 ID를 모르는 미확정 의도는 자동 재주문하지 않고 수동 대조 전까지 LIVE 거래를 차단한다. 토스 주문 기록과 종목·수량·가격·시각을 대조한 경우에만 `/api/v1/live/orders/intents/{client_order_id}/attach`로 주문 ID를 연결할 수 있다. 같은 작업 디렉터리에서 서버를 두 개 실행하면 두 번째 서버 시작을 거부한다.

실거래 전환 전 확인 사항은 [ISSUE.md](../ISSUE.md)를 참조한다.
