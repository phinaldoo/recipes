from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_env: Literal["development", "test", "production"] = "development"
    app_secret_key: str = "development-only-change-me-please-32-chars"
    app_base_url: str = "http://localhost:8080"
    allowed_hosts: str = "localhost,127.0.0.1,testserver"
    force_https: bool = False
    display_timezone: str = "Europe/Berlin"

    pwa_enabled: bool = True
    pwa_name: str = "Rezepte"
    pwa_short_name: str = "Rezepte"
    pwa_start_url: str = "/rezepte"
    pwa_theme_color: str = "#18181b"
    pwa_background_color: str = "#f7f4ee"

    database_url: str = ""
    postgres_db: str = "recipe"
    postgres_user: str = "recipe"
    postgres_password: str = "recipe"
    postgres_host: str = "localhost"
    postgres_port: int = Field(default=5432, ge=1, le=65_535)
    redis_url: str = "redis://localhost:6379/0"
    renderer_url: str = "http://localhost:8001"
    renderer_token: str = "development-renderer-token-change-me"
    renderer_proxy_url: str = ""

    storage_root: Path = Path("data/storage")
    max_upload_mb: int = Field(default=50, ge=1, le=2048)
    max_pdf_pages: int = Field(default=100, ge=1, le=1000)
    max_image_pixels: int = Field(default=60_000_000, ge=1_000_000)
    storage_min_free_mb: int = Field(default=1024, ge=0, le=1_000_000)
    media_recipe_max_count: int = Field(default=200, ge=1, le=100_000)
    media_recipe_max_mb: int = Field(default=512, ge=1, le=1_000_000)
    media_user_max_count: int = Field(default=5000, ge=1, le=1_000_000)
    media_user_max_mb: int = Field(default=10_240, ge=1, le=1_000_000)
    media_global_max_count: int = Field(default=25_000, ge=1, le=10_000_000)
    media_global_max_mb: int = Field(default=51_200, ge=1, le=10_000_000)
    import_source_retention_hours: int = Field(default=720, ge=1, le=24 * 365)
    recipe_json_export_max_assets: int = Field(default=200, ge=1, le=10_000)
    recipe_json_export_max_mb: int = Field(default=256, ge=1, le=10_000)
    recipe_json_export_concurrency: int = Field(default=2, ge=1, le=32)
    category_max_per_recipe: int = Field(default=20, ge=1, le=100)
    comment_max_length: int = Field(default=10_000, ge=1, le=100_000)
    recipe_version_retention: int = Field(default=200, ge=10, le=10_000)

    backup_temp_root: Path = Path("data/backup-temp")
    backup_download_retention_hours: int = Field(default=24, ge=1, le=168)
    max_backup_upload_mb: int = Field(default=2048, ge=1, le=20_480)
    restore_require_password_confirmation: bool = True

    ai_api_key: str = ""
    ai_base_url: str = "https://api.openai.com/v1"
    ai_extraction_model: str = "gpt-5-mini"
    ai_extraction_reasoning_effort: Literal[
        "none", "minimal", "low", "medium", "high", "xhigh", "max"
    ] = "medium"
    ai_image_model: str = "gpt-image-1"
    ai_image_quality: Literal["auto", "low", "medium", "high"] = "auto"
    ai_timeout_seconds: int = Field(default=180, ge=10, le=900)
    ai_max_retries: int = Field(default=3, ge=0, le=10)
    ai_image_generation_enabled: bool = False

    session_cookie_name: str = "rezepte_session"
    session_cookie_secure: bool = False
    session_cookie_samesite: Literal["lax", "strict"] = "lax"
    session_ttl_hours: int = Field(default=168, ge=1, le=24 * 90)
    login_csrf_cookie_name: str = "rezepte_login_csrf"
    login_csrf_ttl_seconds: int = Field(default=600, ge=60, le=3600)
    login_rate_limit_attempts: int = Field(default=10, ge=1, le=100)
    login_rate_limit_ip_attempts: int = Field(default=60, ge=5, le=1000)
    login_rate_limit_window_seconds: int = Field(default=60, ge=10, le=3600)

    @field_validator("app_secret_key")
    @classmethod
    def validate_secret(cls, value: str) -> str:
        if len(value) < 32:
            raise ValueError("APP_SECRET_KEY muss mindestens 32 Zeichen lang sein")
        return value

    @field_validator("app_base_url", "ai_base_url")
    @classmethod
    def validate_http_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Es ist eine absolute HTTP(S)-URL erforderlich")
        return value.rstrip("/")

    @field_validator("display_timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("DISPLAY_TIMEZONE ist keine gültige IANA-Zeitzone") from exc
        return value

    @model_validator(mode="after")
    def production_guards(self) -> Settings:
        if not self.database_url:
            self.database_url = (
                "postgresql+psycopg://"
                f"{quote(self.postgres_user, safe='')}:{quote(self.postgres_password, safe='')}"
                f"@{self.postgres_host}:{self.postgres_port}/{quote(self.postgres_db, safe='')}"
            )
        if self.media_user_max_count < self.media_recipe_max_count:
            raise ValueError(
                "MEDIA_USER_MAX_COUNT darf nicht kleiner als MEDIA_RECIPE_MAX_COUNT sein"
            )
        if self.media_user_max_mb < self.media_recipe_max_mb:
            raise ValueError("MEDIA_USER_MAX_MB darf nicht kleiner als MEDIA_RECIPE_MAX_MB sein")
        if self.media_global_max_count < self.media_user_max_count:
            raise ValueError(
                "MEDIA_GLOBAL_MAX_COUNT darf nicht kleiner als MEDIA_USER_MAX_COUNT sein"
            )
        if self.media_global_max_mb < self.media_user_max_mb:
            raise ValueError("MEDIA_GLOBAL_MAX_MB darf nicht kleiner als MEDIA_USER_MAX_MB sein")
        if self.app_env == "production":
            insecure_markers = ("change", "replace", "development", "example", "please")
            if any(marker in self.app_secret_key.casefold() for marker in insecure_markers):
                raise ValueError("APP_SECRET_KEY muss in Produktion ersetzt werden")
            if not self.force_https:
                raise ValueError("FORCE_HTTPS muss in Produktion aktiv sein")
            if not self.session_cookie_secure:
                raise ValueError("SESSION_COOKIE_SECURE muss in Produktion aktiv sein")
            if not self.app_base_url.startswith("https://"):
                raise ValueError("APP_BASE_URL muss in Produktion HTTPS verwenden")
            if len(self.renderer_token) < 32 or any(
                marker in self.renderer_token.casefold() for marker in insecure_markers
            ):
                raise ValueError("RENDERER_TOKEN muss in Produktion sicher gesetzt sein")
            app_host = urlparse(self.app_base_url).hostname
            if "*" in self.allowed_host_list or app_host not in self.allowed_host_list:
                raise ValueError("ALLOWED_HOSTS muss den Produktionshost explizit enthalten")
            parsed_database = urlparse(self.database_url)
            if (
                not parsed_database.password
                or parsed_database.password == "recipe"
                or parsed_database.hostname in {"localhost", "127.0.0.1"}
            ):
                raise ValueError("DATABASE_URL muss in Produktion sichere Zugangsdaten verwenden")
        return self

    @property
    def allowed_host_list(self) -> list[str]:
        return [item.strip() for item in self.allowed_hosts.split(",") if item.strip()]

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def storage_min_free_bytes(self) -> int:
        return self.storage_min_free_mb * 1024 * 1024

    @property
    def media_recipe_max_bytes(self) -> int:
        return self.media_recipe_max_mb * 1024 * 1024

    @property
    def media_user_max_bytes(self) -> int:
        return self.media_user_max_mb * 1024 * 1024

    @property
    def media_global_max_bytes(self) -> int:
        return self.media_global_max_mb * 1024 * 1024

    @property
    def recipe_json_export_max_bytes(self) -> int:
        return self.recipe_json_export_max_mb * 1024 * 1024

    @property
    def max_backup_upload_bytes(self) -> int:
        return self.max_backup_upload_mb * 1024 * 1024

    def ensure_directories(self) -> None:
        self.storage_root.mkdir(parents=True, exist_ok=True)
        self.backup_temp_root.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()
