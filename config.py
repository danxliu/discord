from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    discord_token: str
    pelican_application_key: str = ""
    pelican_client_key: str = ""
    pelican_base_url: str = ""

    ai_api_key: str
    ai_base_url: str = "https://openrouter.ai/api/v1"
    ai_model: str = "openai/gpt-4o"
    ai_system_prompt_path: str = "prompts/system.md"
    ai_max_iterations: int = 10
    ai_max_history_turns: int = 20
    ai_channel_history_limit: int = 10

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
