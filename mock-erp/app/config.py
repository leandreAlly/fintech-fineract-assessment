from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg://erp:erp@localhost:5432/erp"


settings = Settings()
