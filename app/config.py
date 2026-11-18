import os
from dataclasses import dataclass


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    verify_token: str
    app_secret: str
    page_id: str
    page_access_token: str
    graph_version: str
    webhook_path: str
    database_url: str
    seed_file: str
    currency: str
    max_attempts: int
    backoff_base_seconds: int
    poll_interval: float
    send_timeout: float
    dry_run: bool
    default_catalog_id: str
    admin_password: str

    @staticmethod
    def load() -> "Settings":
        return Settings(
            verify_token=_get("FACEBOOK_VERIFY_TOKEN"),
            app_secret=_get("META_APP_SECRET"),
            page_id=_get("FACEBOOK_PAGE_ID"),
            page_access_token=_get("FACEBOOK_PAGE_ACCESS_TOKEN"),
            graph_version=_get("META_GRAPH_VERSION", "v21.0"),
            webhook_path=_get("FACEBOOK_WEBHOOK_PATH", "/webhooks/facebook"),
            database_url=_get("DATABASE_URL", "sqlite:///./local.db"),
            seed_file=_get("SEED_FILE", ""),
            currency=_get("CURRENCY", "VND"),
            max_attempts=int(_get("MAX_ATTEMPTS", "6")),
            backoff_base_seconds=int(_get("BACKOFF_BASE_SECONDS", "10")),
            poll_interval=float(_get("WORKER_POLL_INTERVAL", "1.0")),
            send_timeout=float(_get("SEND_TIMEOUT", "15")),
            # DRY_RUN=1: never call Meta, log only (safe local testing)
            dry_run=_get("DRY_RUN", "0") == "1",
            # catalog used for any post without its own facebook_posts row
            default_catalog_id=_get("DEFAULT_CATALOG_ID", ""),
            admin_password=_get("ADMIN_PASSWORD", ""),
        )
