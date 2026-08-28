from functools import lru_cache
from pathlib import Path

from pydantic import AnyUrl, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    telegram_bot_token: str = Field(default="", alias="TELEGRAM_BOT_TOKEN")
    max_bot_token: str = Field(default="", alias="MAX_BOT_TOKEN")
    max_bot_username: str = Field(default="", alias="MAX_BOT_USERNAME")
    miniapp_url: AnyUrl | None = Field(default=None, alias="MINIAPP_URL")
    telegram_miniapp_url_override: AnyUrl | None = Field(
        default=None,
        alias="TELEGRAM_MINIAPP_URL",
    )
    max_miniapp_url_override: AnyUrl | None = Field(default=None, alias="MAX_MINIAPP_URL")
    database_url: str = Field(
        default="postgresql+asyncpg://michelangelo:michelangelo@postgres:5432/michelangelo",
        alias="DATABASE_URL",
    )
    admin_username: str = Field(default="admin", alias="ADMIN_USERNAME")
    admin_password: str = Field(default="change-me", alias="ADMIN_PASSWORD")
    admin_base_url: str = Field(default="", alias="ADMIN_BASE_URL")
    order_notification_telegram_chat_ids: str = Field(
        default="",
        alias="ORDER_NOTIFICATION_TELEGRAM_CHAT_IDS",
    )
    order_notification_max_user_ids: str = Field(
        default="",
        alias="ORDER_NOTIFICATION_MAX_USER_IDS",
    )
    order_notification_max_chat_ids: str = Field(
        default="",
        alias="ORDER_NOTIFICATION_MAX_CHAT_IDS",
    )
    uploads_dir: Path = Field(default=Path("uploads"), alias="UPLOADS_DIR")
    readyscript_webhook_secret: str = Field(default="", alias="READYSCRIPT_WEBHOOK_SECRET")
    # Общий секрет HMAC-подписи вебхуков модуля michelangelo (ReadyScript → админка).
    rs_module_secret: str = Field(default="", alias="RS_MODULE_SECRET")
    rs_signature_tolerance_seconds: int = Field(
        default=300,
        alias="RS_SIGNATURE_TOLERANCE_SECONDS",
    )
    readyscript_script_path: Path = Field(
        default=Path("readyscript-orders/script.py"),
        alias="READYSCRIPT_SCRIPT_PATH",
    )
    readyscript_api_base: str = Field(
        default="https://michelangelo-lab.rscms.ru/api-6cdywf0i/methods",
        alias="RS_API_BASE",
    )
    readyscript_client_id: str = Field(default="", alias="RS_CLIENT_ID")
    readyscript_client_secret: str = Field(default="", alias="RS_CLIENT_SECRET")
    readyscript_username: str = Field(default="", alias="RS_USERNAME")
    readyscript_password: str = Field(default="", alias="RS_PASSWORD")
    readyscript_page_size: int = Field(default=100, alias="RS_PAGE_SIZE")
    readyscript_interval_seconds: int = Field(default=60, alias="RS_INTERVAL_SECONDS")
    readyscript_fetch_order_details: bool = Field(default=True, alias="RS_FETCH_ORDER_DETAILS")
    readyscript_fetch_users: bool = Field(default=True, alias="RS_FETCH_USERS")
    readyscript_request_delay_seconds: float = Field(
        default=0.05,
        alias="RS_REQUEST_DELAY_SECONDS",
    )
    readyscript_request_timeout: int = Field(default=30, alias="RS_REQUEST_TIMEOUT")
    max_api_base_url: AnyUrl = Field(
        default="https://platform-api2.max.ru",
        alias="MAX_API_BASE_URL",
    )
    max_poll_timeout_seconds: int = Field(default=30, alias="MAX_POLL_TIMEOUT_SECONDS")

    @property
    def telegram_miniapp_url(self) -> AnyUrl:
        url = self.telegram_miniapp_url_override or self.miniapp_url
        if url is None:
            raise RuntimeError("TELEGRAM_MINIAPP_URL or MINIAPP_URL is not set")
        return url

    @property
    def max_miniapp_url(self) -> AnyUrl:
        url = self.max_miniapp_url_override or self.miniapp_url
        if url is None:
            raise RuntimeError("MAX_MINIAPP_URL or MINIAPP_URL is not set")
        return url

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
