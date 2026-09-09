"""Configuracao da aplicação, carregada de variáveis de ambiente / .env."""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="PNCP_", extra="ignore"
    )

    database_url: str = Field(
        default="postgresql+psycopg://postgres:postgres@localhost:5432/pncp",
        description="URL SQLAlchemy do Postgres.",
    )

    base_url: str = "https://pncp.gov.br"

    # Janelas de varredura (em dias), conforme a regra de coleta.
    atas_days_back: int = 364
    atas_days_ahead: int = 90
    contratos_days_back: int = 400

    # Corte de atualidade do preço: assinatura nos últimos N dias.
    signature_max_age_days: int = 365

    # Contrato derivado de ata ja coletada: por padrao e mantido e apenas
    # marcado (derived_from_ata). Ligue para descarta-lo da amostra.
    discard_derived_contracts: bool = False

    # Vigência acima disso marca o registro como suspeito (não descarta).
    suspicious_validity_days: int = 730

    # A API rejeita tamanhoPagina fora de [10, 500].
    page_size: int = Field(default=100, ge=10, le=500)
    request_delay: float = 0.5  # segundos entre requisições
    timeout: float = 60.0
    max_retries: int = 8
    max_backoff: float = 60.0

    # Recortes opcionais da consulta.
    cnpj: str | None = None
    codigo_unidade_administrativa: str | None = None


settings = Settings()
