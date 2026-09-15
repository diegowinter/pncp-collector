"""Modelo de dados (SQLAlchemy 2.0) das atas e contratos coletados.

Convencao de nomes: as colunas que vem do PNCP mantem o nome da API, apenas
convertido de camelCase para snake_case (`dataAssinatura` -> `data_assinatura`),
inclusive as de objetos aninhados, que carregam o caminho no nome
(`orgaoEntidade.razaoSocial` -> `orgao_entidade_razao_social`). So os campos de
controle nossos ficam em ingles: `collected_at`, `updated_at`, `raw`,
`suspicious`, `derived_from_ata` e afins.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class ControlMixin:
    """Campos de controle nossos, sem contrapartida no PNCP."""

    collected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    suspicious: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    suspicious_reasons: Mapped[list[str] | None] = mapped_column(JSONB)
    raw: Mapped[dict] = mapped_column(JSONB)


class Ata(Base, ControlMixin):
    """Ata de registro de precos. GET /api/consulta/v1/atas."""

    __tablename__ = "atas"

    numero_controle_pncp_ata: Mapped[str] = mapped_column(String(80), primary_key=True)
    numero_controle_pncp_compra: Mapped[str | None] = mapped_column(
        String(80), index=True
    )
    numero_ata_registro_preco: Mapped[str | None] = mapped_column(String(120))
    ano_ata: Mapped[int | None] = mapped_column(Integer)

    data_assinatura: Mapped[date] = mapped_column(Date, index=True)
    vigencia_inicio: Mapped[date | None] = mapped_column(Date)
    vigencia_fim: Mapped[date | None] = mapped_column(Date)
    data_publicacao_pncp: Mapped[date | None] = mapped_column(Date, index=True)
    data_inclusao: Mapped[date | None] = mapped_column(Date)
    data_atualizacao: Mapped[date | None] = mapped_column(Date)
    data_atualizacao_global: Mapped[date | None] = mapped_column(Date)

    cancelado: Mapped[bool] = mapped_column(Boolean, default=False)
    data_cancelamento: Mapped[date | None] = mapped_column(Date)

    objeto_contratacao: Mapped[str | None] = mapped_column(Text)

    cnpj_orgao: Mapped[str | None] = mapped_column(String(20), index=True)
    nome_orgao: Mapped[str | None] = mapped_column(Text)
    cnpj_orgao_subrogado: Mapped[str | None] = mapped_column(String(20))
    nome_orgao_subrogado: Mapped[str | None] = mapped_column(Text)
    codigo_unidade_orgao: Mapped[str | None] = mapped_column(String(40))
    nome_unidade_orgao: Mapped[str | None] = mapped_column(Text)
    codigo_unidade_orgao_subrogado: Mapped[str | None] = mapped_column(String(40))
    nome_unidade_orgao_subrogado: Mapped[str | None] = mapped_column(Text)

    usuario: Mapped[str | None] = mapped_column(Text)
    possibilidade_adesao: Mapped[bool | None] = mapped_column(Boolean)


class Contrato(Base, ControlMixin):
    """Contrato. GET /api/consulta/v1/contratos.

    A API nao devolve `cancelado`/`dataCancelamento` para contratos, entao essas
    colunas nao existem aqui.
    """

    __tablename__ = "contratos"

    numero_controle_pncp: Mapped[str] = mapped_column(String(80), primary_key=True)
    numero_controle_pncp_compra: Mapped[str | None] = mapped_column(
        String(80), index=True
    )
    numero_controle_pncp_ata: Mapped[str | None] = mapped_column(String(80), index=True)
    numero_contrato_empenho: Mapped[str | None] = mapped_column(String(120))
    ano_contrato: Mapped[int | None] = mapped_column(Integer)
    sequencial_contrato: Mapped[int | None] = mapped_column(Integer)
    processo: Mapped[str | None] = mapped_column(String(120))

    data_assinatura: Mapped[date] = mapped_column(Date, index=True)
    data_vigencia_inicio: Mapped[date | None] = mapped_column(Date)
    data_vigencia_fim: Mapped[date | None] = mapped_column(Date)
    data_publicacao_pncp: Mapped[date | None] = mapped_column(Date, index=True)
    data_atualizacao: Mapped[date | None] = mapped_column(Date)
    data_atualizacao_global: Mapped[date | None] = mapped_column(Date)

    objeto_contrato: Mapped[str | None] = mapped_column(Text)
    informacao_complementar: Mapped[str | None] = mapped_column(Text)

    ni_fornecedor: Mapped[str | None] = mapped_column(String(30), index=True)
    tipo_pessoa: Mapped[str | None] = mapped_column(String(2))
    nome_razao_social_fornecedor: Mapped[str | None] = mapped_column(Text)

    valor_inicial: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    valor_parcela: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    valor_global: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    valor_acumulado: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    numero_parcelas: Mapped[int | None] = mapped_column(Integer)
    receita: Mapped[bool | None] = mapped_column(Boolean)

    orgao_entidade_cnpj: Mapped[str | None] = mapped_column(String(20), index=True)
    orgao_entidade_razao_social: Mapped[str | None] = mapped_column(Text)
    orgao_entidade_poder_id: Mapped[str | None] = mapped_column(String(2))
    orgao_entidade_esfera_id: Mapped[str | None] = mapped_column(String(2))

    unidade_orgao_codigo_unidade: Mapped[str | None] = mapped_column(String(40))
    unidade_orgao_nome_unidade: Mapped[str | None] = mapped_column(Text)
    unidade_orgao_uf_sigla: Mapped[str | None] = mapped_column(String(2))
    unidade_orgao_municipio_nome: Mapped[str | None] = mapped_column(Text)
    unidade_orgao_codigo_ibge: Mapped[str | None] = mapped_column(String(10))

    tipo_contrato_id: Mapped[int | None] = mapped_column(Integer)
    tipo_contrato_nome: Mapped[str | None] = mapped_column(Text)
    categoria_processo_id: Mapped[int | None] = mapped_column(Integer)
    categoria_processo_nome: Mapped[str | None] = mapped_column(Text)

    usuario_nome: Mapped[str | None] = mapped_column(Text)

    # Controle nosso: o contrato deriva de uma ata que tambem esta na base.
    derived_from_ata: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    derivation_match: Mapped[str | None] = mapped_column(String(20))


Index("ix_atas_assinatura_orgao", Ata.data_assinatura, Ata.cnpj_orgao)
Index(
    "ix_contratos_assinatura_orgao",
    Contrato.data_assinatura,
    Contrato.orgao_entidade_cnpj,
)


class CollectionRun(Base):
    """Registro de cada execucao. Tabela nossa: tudo em ingles."""

    __tablename__ = "collection_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dataset: Mapped[str] = mapped_column(String(20))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    window_start: Mapped[date | None] = mapped_column(Date)
    window_end: Mapped[date | None] = mapped_column(Date)
    stats: Mapped[dict | None] = mapped_column(JSONB)
