from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    discord_token: str
    pelican_application_key: str
    pelican_client_key: str
    pelican_base_url: str

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
