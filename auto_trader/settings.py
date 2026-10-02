from decimal import Decimal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    trading_mode: str = "PAPER"
    live_trading_enabled: bool = False
    live_order_max_retries: int = 2
    live_order_retry_backoff_seconds: Decimal = Decimal("1")
    paper_initial_cash: Decimal = Decimal("10000000")
    recommended_trade_ratio: Decimal = Decimal("0.10")
    paper_commission_rate: Decimal = Decimal("0.00015")
    stop_loss_rate: Decimal = Decimal("-0.07")
    take_profit_rate: Decimal = Decimal("0.15")
    partial_take_profit_rate: Decimal = Decimal("0.10")
    partial_take_profit_fraction: Decimal = Decimal("0.50")
    trailing_stop_activation_rate: Decimal = Decimal("0.05")
    trailing_stop_rate: Decimal = Decimal("0.05")
    max_holding_days: int = 20
    force_flatten_at_close: bool = False
    flatten_before_close_minutes: int = 10
    paper_max_slippage_rate: Decimal = Decimal("0.02")
    paper_simulated_slippage_rate: Decimal = Decimal("0.001")
    paper_min_average_daily_volume: Decimal = Decimal("10000")
    paper_min_average_daily_turnover: Decimal = Decimal("100000000")
    daily_loss_limit_rate: Decimal = Field(default=Decimal("0.03"), gt=0, le=1)
    max_order_amount: Decimal = Field(default=Decimal("4000000"), gt=0)
    max_symbol_exposure_amount: Decimal = Field(default=Decimal("4000000"), gt=0)
    max_symbol_quantity: Decimal = Field(default=Decimal("100"), gt=0)
    max_portfolio_exposure_amount: Decimal = Field(default=Decimal("7000000"), gt=0)
    max_open_positions: int = Field(default=5, ge=1)
    live_fee_reserve_rate: Decimal = Field(default=Decimal("0.01"), ge=0, le=Decimal("0.05"))
    toss_client_id: str = ""
    toss_client_secret: str = ""
    toss_account_seq: str = ""
    toss_api_base: str = "https://openapi.tossinvest.com"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", validate_assignment=True)


settings = Settings()
