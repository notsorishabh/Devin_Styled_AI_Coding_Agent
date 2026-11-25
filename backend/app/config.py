from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"), extra="ignore", populate_by_name=True
    )

    sandbox_provider: str = "docker"
    database_url: str = "postgresql+asyncpg://devin:devin@127.0.0.1:5433/devin"
    github_client_id: str = ""
    github_client_secret: str = ""
    github_oauth_redirect_uri: str = "http://localhost:5173/auth/github/callback"
    jwt_secret: str = "change-me"
    jwt_algorithm: str = "HS256"
    frontend_origin: str = "http://localhost:5173"
    token_encryption_key: str = ""
    session_secret: str = "change-me-session"
    cookie_secure: bool = False
    access_token_ttl_seconds: int = 15 * 60
    refresh_token_ttl_seconds: int = 7 * 24 * 60 * 60

    sandbox_cpus: float = 1.0
    sandbox_memory: str = "1g"
    sandbox_timeout_seconds: int = 600
    sandbox_image: str = "devin-sandbox:v2"
    sandbox_reap_interval_seconds: int = 15
    shell_buffer_max_events: int = 2000
    llm_api_key: str = Field(
        default="", validation_alias=AliasChoices("LLM_API_KEY", "OPENAI_API_KEY")
    )
    llm_model: str = "gpt-4o-mini"
    llm_base_url: str = ""
    llm_provider: str = "fake"
    agent_max_iterations: int = 25


settings = Settings()
