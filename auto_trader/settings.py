from decimal import Decimal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    trading_mode: str = "PAPER"
    paper_initial_cash: Decimal = Decimal("10000000")
    recommended_trade_ratio: Decimal = Decimal("0.10")
    toss_client_id: str = ""
    toss_client_secret: str = ""
    toss_account_seq: str = ""
    toss_api_base: str = "https://openapi.tossinvest.com"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
