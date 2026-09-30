from decimal import Decimal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    trading_mode: str = "PAPER"
    live_trading_enabled: bool = False
    live_order_max_retries: int = 2
    live_order_retry_backoff_seconds: Decimal = Decimal("1")
    paper_initial_cash: Decimal = Decimal("10000000")
    recommended_trade_ratio: Decimal = Decimal("0.10")
    paper_commission_rate: Decimal = Decimal("0.00015")
    toss_client_id: str = ""
    toss_client_secret: str = ""
    toss_account_seq: str = ""
    toss_api_base: str = "https://openapi.tossinvest.com"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
