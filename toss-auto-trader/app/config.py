import os
from dataclasses import dataclass

try:
    from dotenv import load_dotenv
except ImportError:  # 설정 파일 없이도 전략/DB 단위 테스트를 실행할 수 있게 한다.
    def load_dotenv() -> bool:
        return False

load_dotenv()


@dataclass(frozen=True)
class Settings:
    trading_mode: str = os.getenv("TRADING_MODE", "PAPER").upper()
    database_path: str = os.getenv("DATABASE_PATH", "data/trader.db")
    toss_client_id: str = os.getenv("TOSS_CLIENT_ID", "")
    toss_client_secret: str = os.getenv("TOSS_CLIENT_SECRET", "")
    toss_account_seq: str = os.getenv("TOSS_ACCOUNT_SEQ", "")
    toss_api_base_url: str = os.getenv("TOSS_API_BASE_URL", "https://openapi.tossinvest.com").rstrip("/")
    paper_initial_cash: int = int(os.getenv("PAPER_INITIAL_CASH", "10000000"))
    paper_buy_fee_rate: float = float(os.getenv("PAPER_BUY_FEE_RATE", "0"))
    paper_sell_fee_rate: float = float(os.getenv("PAPER_SELL_FEE_RATE", "0"))
    paper_sell_tax_rate: float = float(os.getenv("PAPER_SELL_TAX_RATE", "0"))
    paper_auto_exit_enabled: bool = os.getenv("PAPER_AUTO_EXIT_ENABLED", "true").lower() == "true"
    paper_stop_loss_rate: float = float(os.getenv("PAPER_STOP_LOSS_RATE", "0.05"))
    paper_take_profit_rate: float = float(os.getenv("PAPER_TAKE_PROFIT_RATE", "0.10"))
    paper_auto_exit_interval_seconds: int = int(os.getenv("PAPER_AUTO_EXIT_INTERVAL_SECONDS", "60"))
    paper_auto_buy_enabled: bool = os.getenv("PAPER_AUTO_BUY_ENABLED", "true").lower() == "true"
    paper_auto_buy_interval_seconds: int = int(os.getenv("PAPER_AUTO_BUY_INTERVAL_SECONDS", "900"))
    paper_auto_buy_budget: int = int(os.getenv("PAPER_AUTO_BUY_BUDGET", "5000000"))
    paper_auto_buy_min_score: int = int(os.getenv("PAPER_AUTO_BUY_MIN_SCORE", "80"))
    paper_max_positions: int = int(os.getenv("PAPER_MAX_POSITIONS", "3"))
    paper_max_order_amount: int = int(os.getenv("PAPER_MAX_ORDER_AMOUNT", "100000"))
    paper_daily_loss_limit_rate: float = float(os.getenv("PAPER_DAILY_LOSS_LIMIT_RATE", "0.03"))

    @property
    def toss_configured(self) -> bool:
        return bool(self.toss_client_id and self.toss_client_secret)

    def validate(self) -> None:
        if self.trading_mode != "PAPER":
            raise RuntimeError("1차 버전은 TRADING_MODE=PAPER만 허용합니다.")
        if not 0 < self.paper_stop_loss_rate < 1 or not 0 < self.paper_take_profit_rate < 1:
            raise RuntimeError("손절·익절 비율은 0과 1 사이여야 합니다.")
        if not 1 <= self.paper_auto_buy_min_score <= 100:
            raise RuntimeError("자동매수 최소 점수는 1~100점이어야 합니다.")
        if self.paper_max_positions < 1 or self.paper_max_order_amount < 1:
            raise RuntimeError("자동매수 보유·주문 한도가 올바르지 않습니다.")
        if not 0 < self.paper_daily_loss_limit_rate < 1:
            raise RuntimeError("일일 손실 제한 비율은 0과 1 사이여야 합니다.")


settings = Settings()
settings.validate()
