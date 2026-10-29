from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg://integration:integration@localhost:5432/integration"
    fineract_base_url: str = "https://localhost:8443/fineract-provider/api/v1"
    fineract_tenant: str = "default"
    fineract_username: str = "mifos"
    fineract_password: str = "password"
    fineract_verify_tls: bool = False
    erp_base_url: str = "http://localhost:8081"
    http_timeout_seconds: float = 10.0


settings = Settings()
