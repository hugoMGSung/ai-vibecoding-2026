# v0.1 실행

```powershell
python -m pip install -r requirements.txt
python -m auto_trader
```

브라우저에서 `http://127.0.0.1:8000/docs`를 열면 Swagger UI를 사용할 수 있다.

v0.1은 토스증권 실거래 API를 호출하지 않는 `PAPER`/`DRY_RUN` 프로토타입이다. 가격을 등록한 뒤 주문을 요청한다.

```powershell
Invoke-RestMethod -Method Put 'http://127.0.0.1:8000/api/v1/market/prices/005930?price=70000'
Invoke-RestMethod -Method Post 'http://127.0.0.1:8000/api/v1/orders' -ContentType 'application/json' -Body '{"symbol":"005930","side":"BUY","quantity":"10","price":"70000"}'
Invoke-RestMethod 'http://127.0.0.1:8000/api/v1/portfolio'
```
