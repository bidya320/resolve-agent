import os
from dataclasses import dataclass


def _e(key, default):
    return os.environ.get(key, default)


@dataclass(frozen=True)
class Settings:
    db_path: str = _e("DB_PATH", "support.db")
    model: str = _e("AGENT_MODEL", "claude-sonnet-5-5")
    admin_key: str = _e("ADMIN_API_KEY", "")                    # empty = admin API disabled
    allow_body_email: bool = _e("ALLOW_BODY_EMAIL", "1") == "1"  # dev only; prod: identity comes from your auth gateway
    max_auto_refund: float = float(_e("MAX_AUTO_REFUND", "100"))  # above this a human must approve
    late_days_for_remedy: int = int(_e("LATE_DAYS_FOR_REMEDY", "3"))
    max_steps: int = int(_e("MAX_AGENT_STEPS", "8"))
    rate_limit_per_min: int = int(_e("RATE_LIMIT_PER_MIN", "20"))
    orders_api_url: str = _e("ORDERS_API_URL", "")               # empty = built-in local orders table
    orders_api_token: str = _e("ORDERS_API_TOKEN", "")
    kb_dir: str = _e("KB_DIR", "data/kb")


def get_settings() -> Settings:
    return Settings()
