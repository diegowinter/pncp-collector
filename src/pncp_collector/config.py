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
    # Intervalo mínimo entre o início de duas requisições de consulta. Com
    # concorrência > 1 ele é o teto global (compartilhado entre as threads),
    # não um delay por thread.
    request_delay: float = 0.5  # segundos entre requisições
    timeout: float = 60.0
    # Tentativas por requisicao em 429/5xx/transporte; None = sem limite.
    max_retries: int | None = Field(default=None, ge=1)
    max_backoff: float = 60.0

    # Dias buscados em paralelo na fase 1. A API de consulta e rapida mas
    # devolve 429 com frequencia (docs/API_DETALHE.md, § 1.6), entao aqui a
    # concorrencia serve para esconder latencia, nao para multiplicar o ritmo:
    # `request_delay` continua limitando a taxa global.
    list_concurrency: int = Field(default=1, ge=1, le=16)

    # Fase 2 (detalhamento). A API de detalhe e lenta (~8 s por chamada) mas
    # nao limita taxa como a de consulta; ver docs/API_DETALHE.md, § 1.6.
    detail_request_delay: float | None = None  # None = request_delay
    detail_page_size: int = Field(default=500, ge=10, le=500)  # /itens

    # Tipos de arquivo (tipoDocumentoNome) que contam como "arquivo alvo",
    # separados por virgula; vazio = qualquer arquivo conta. Os defaults vem
    # da medicao em docs/API_DETALHE.md, § 1.2: as atas do orgao amostrado
    # sao 100% "Outros Documentos", e contrato-por-empenho so tem a nota.
    target_file_types_ata: str = ""
    target_file_types_contrato: str = "Contrato,Nota de Empenho"

    # Documento/compra em erro deixa de ser revisitado apos N tentativas;
    # None = revisitado em toda execucao.
    detail_max_attempts: int | None = Field(default=None, ge=1)

    # Threads nas chamadas de detalhe (a latencia e do servidor, nao throttling).
    detail_concurrency: int = Field(default=1, ge=1, le=8)

    @property
    def effective_detail_delay(self) -> float:
        if self.detail_request_delay is None:
            return self.request_delay
        return self.detail_request_delay

    # Fase 3 (triagem por objeto). Endpoint de decisoes do OpenRouter; a chave
    # NUNCA vai no codigo nem no repositorio.
    openrouter_api_key: str | None = None
    openrouter_base_url: str = "https://openrouter.ai"
    triage_model: str = "typesafe/jev-1.14"
    # Chamadas em paralelo. O endpoint e rapido (~0,6 s) e sem throttling
    # observado ate 8.
    triage_concurrency: int = Field(default=8, ge=1, le=32)
    # Probabilidade minima de "potencial" para a compra ir para a fase 2.
    # 0,45 vem da validacao humana de 2026-11-13 (docs/validacao): pega 8 dos 9
    # "sim" contra 6 com 0,5, e so 2% a mais de compras entram.
    triage_potential_threshold: float = Field(default=0.45, ge=0.0, le=1.0)
    # Probabilidade minima para um grupo entrar na lista de sugeridos.
    triage_group_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    # Compra em erro deixa de ser revisitada apos N tentativas; None = sempre.
    triage_max_attempts: int | None = Field(default=None, ge=1)
    # Teto de gasto por execucao, em dolares; None = sem teto.
    triage_max_cost_usd: float | None = Field(default=None, ge=0.0)

    # Fase 5 (classificador local de itens). O modelo e derivado de item_labels e do
    # codigo: nao vai no repositorio; qualquer maquina com o banco o refaz em minutos.
    models_dir: str = "models"
    # Gabarito humano: essas descricoes ficam fora do treino e medem o resultado.
    item_validation_csv: str = "docs/validacao/amostra_itens_2026-11-14_claude.csv"
    # "balanced" favorece recall nas classes raras (filtro do passo C); "none" favorece
    # precisao. Medido em 2026-11-14: balanced P=0,83 R=0,98; none P=0,91 R=0,96.
    item_class_weight: str = "balanced"
    # Probabilidade minima de "e de interesse" (1 - P(outros)) para o item contar.
    # So semeia o banco na primeira vez: o valor em vigor fica na tabela `settings`
    # (comando `limiar`), para valer em toda maquina e poder mudar por painel.
    item_interest_threshold: float = Field(default=0.5, ge=0.0, le=1.0)

    # Recortes opcionais da consulta.
    cnpj: str | None = None
    codigo_unidade_administrativa: str | None = None


settings = Settings()
