import os
from pathlib import Path
from zoneinfo import ZoneInfo

DATA_DIR = Path(os.getenv("DATA_DIR", "./data"))
DB_PATH = DATA_DIR / "monitor.db"

SECRET_KEY = os.getenv("SECRET_KEY", "")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
# Set to true when the panel is served over HTTPS (e.g. behind Caddy/Nginx).
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() in ("1", "true", "yes")
APP_TZ = ZoneInfo(os.getenv("APP_TZ", "Asia/Hong_Kong"))

# Probe parameters: everything else (reasoning effort, ...) is left to model defaults.
TEXT_PROMPT = "Reply with only the word: OK"
IMAGE_PROMPT = "A red apple on a white table"
# Fixed so every image check does the same amount of work; "auto" defaults make generation time swing 20s~70s.
IMAGE_QUALITY = "low"
IMAGE_SIZE = "1024x1024"
TEXT_MAX_TOKENS = 1024
TEXT_TIMEOUT_SECONDS = 120
IMAGE_TIMEOUT_SECONDS = 300

DEFAULT_SETTINGS = {
    "default_interval_minutes": "30",
    "ttft_threshold_pct": "50",
    "baseline_window": "20",
    "baseline_min_samples": "5",
    "retry_count": "3",
    "retry_gap_seconds": "5",
    "retention_days": "90",
    "smtp_host": "",
    "smtp_port": "465",
    "smtp_security": "ssl",
    "smtp_username": "",
    "smtp_password_enc": "",
    "smtp_from": "",
    "smtp_to": "",
    "feishu_webhook_enc": "",
    "feishu_secret_enc": "",
}


def validate() -> None:
    missing = [n for n, v in (("SECRET_KEY", SECRET_KEY), ("ADMIN_PASSWORD", ADMIN_PASSWORD)) if not v]
    if missing:
        raise RuntimeError(f"缺少必需的环境变量: {', '.join(missing)}（参见 .env.example）")
    if len(SECRET_KEY) < 16:
        raise RuntimeError("SECRET_KEY 长度至少 16 个字符")
    if len(ADMIN_PASSWORD) < 8:
        raise RuntimeError("ADMIN_PASSWORD 长度至少 8 个字符")
