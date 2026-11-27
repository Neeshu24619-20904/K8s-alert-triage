from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    kube_context: str = "minikube"
    llm_provider: str = "ollama"
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5-coder:7b"
    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    dry_run_remediation: bool = True
    git_repo_path: str = "."
    webhook_notify_url: str = ""


settings = Settings()