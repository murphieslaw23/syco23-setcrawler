from functools import lru_cache
from typing import Literal
from uuid import UUID

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "syco23-setcrawler-api"
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    database_url: str = "postgresql://postgres:postgres@db:5432/syco23"
    cors_origins: str = "http://localhost:3000"
    youtube_api_key: str = ""
    audius_api_bearer_token: str = Field(default="", max_length=4096)
    archive_org_enabled: bool = False
    mixcloud_enabled: bool = False
    audius_enabled: bool = False
    rss_enabled: bool = False
    rss_trusted_feeds_json: str = Field(default="", max_length=65_536)
    scraper_user_agent: str = "syco23-setcrawler/0.1 (+contact: local@example.com)"
    scraper_request_delay_ms: int = Field(default=5_000, ge=5_000, le=10_000)
    ftm_scraper_enabled: bool = False
    ftm_max_pages_per_run: int = Field(default=25, ge=1, le=25)
    yt_dlp_bin: str = "yt-dlp"
    provider_mode: Literal["fixture", "live"] = "fixture"
    provider_request_timeout_seconds: float = Field(
        default=20,
        gt=0,
        le=120,
    )
    provider_output_limit_bytes: int = Field(
        default=1_048_576,
        ge=1,
        le=1_048_576,
    )
    audio_storage_enabled: bool = False
    minio_endpoint: str = Field(default="minio:9000", min_length=1, max_length=255)
    minio_access_key: SecretStr = SecretStr("")
    minio_secret_key: SecretStr = SecretStr("")
    minio_secure: bool = False
    audio_max_object_bytes: int = Field(
        default=4_294_967_296,
        ge=1,
        le=5_368_709_120,
    )
    audio_multipart_part_size_bytes: int = Field(
        default=16_777_216,
        ge=5_242_880,
        le=5_368_709_120,
    )
    environment: Literal["fixture", "local", "production"] = "local"
    repository_mode: Literal["memory", "postgres"] = "postgres"
    auth_mode: Literal["local", "supabase"] = "local"
    supabase_url: str = ""
    supabase_anon_key: str = ""
    supabase_jwt_audience: str = Field(
        default="authenticated",
        min_length=1,
    )
    redis_url: str = "redis://redis:6379/0"
    job_claim_ttl_seconds: int = Field(default=300, ge=1)
    job_redrive_interval_seconds: int = Field(default=60, ge=5)
    job_redrive_batch_size: int = Field(default=100, ge=1, le=1_000)
    service_component: str = Field(default="api", min_length=1, max_length=80)
    health_probe_timeout_seconds: float = Field(default=1.0, gt=0, le=10)
    health_probe_cache_seconds: int = Field(default=10, ge=1, le=60)
    beat_stale_after_seconds: int = Field(default=180, ge=60, le=3_600)
    dead_letter_alert_threshold: int = Field(default=1, ge=1)
    stuck_job_alert_threshold: int = Field(default=1, ge=1)
    redrive_failure_alert_threshold: int = Field(default=1, ge=1)
    provider_failure_alert_threshold: int = Field(default=1, ge=1)
    local_user_id: UUID = UUID("00000000-0000-4000-8000-000000000023")
    local_user_role: Literal["viewer", "editor", "admin"] = "admin"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @model_validator(mode="after")
    def validate_production_auth(self) -> "Settings":
        if (
            self.repository_mode == "memory"
            and self.environment != "fixture"
        ):
            raise ValueError(
                "REPOSITORY_MODE=memory is only allowed in fixture mode"
            )
        if self.environment == "production" and self.auth_mode == "local":
            raise ValueError("AUTH_MODE=local is not allowed in production")
        if self.environment == "production" and (
            not self.supabase_url or not self.supabase_anon_key
        ):
            raise ValueError("Supabase URL and anonymous key are required in production")
        if self.audio_storage_enabled and (
            not self.minio_endpoint.strip()
            or not self.minio_access_key.get_secret_value()
            or not self.minio_secret_key.get_secret_value()
        ):
            raise ValueError(
                "MinIO endpoint and server-side credentials are required when audio storage is enabled"
            )
        return self

    @property
    def allowed_origins(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def supabase_jwt_issuer(self) -> str:
        """Canonical issuer derived from the configured Supabase project URL."""
        return f"{self.supabase_url.rstrip('/')}/auth/v1"


@lru_cache
def get_settings() -> Settings:
    return Settings()
