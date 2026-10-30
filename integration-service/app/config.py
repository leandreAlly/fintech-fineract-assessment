from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg://integration:integration@localhost:5432/integration"
    fineract_base_url: str = "https://localhost:8443/fineract-provider/api/v1"
    fineract_tenant: str = "default"
    fineract_username: str = "integration_service"
    fineract_password: str = "Integr4tion-Svc-26!"
    fineract_verify_tls: bool = False
    fineract_office_external_id: str = "OFFICE-KGL-MAIN"
    erp_base_url: str = "http://localhost:8081"
    http_timeout_seconds: float = 10.0
    sync_max_attempts: int = 10
    sync_backoff_base_seconds: float = 2.0
    sync_backoff_max_seconds: float = 60.0
    sync_retry_interval_seconds: float = 5.0


settings = Settings()
