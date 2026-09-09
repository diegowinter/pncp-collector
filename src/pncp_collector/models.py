"""Modelo de dados (SQLAlchemy 2.0) das atas e contratos coletados."""

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


class TimestampMixin:
    collected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Ata(Base, TimestampMixin):
    """Ata de registro de precos. Chave natural: numeroControlePNCPAta."""

    __tablename__ = "atas"

    pncp_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    compra_pncp_id: Mapped[str | None] = mapped_column(String(80), index=True)
    numero_ata: Mapped[str | None] = mapped_column(String(120))
    ano: Mapped[int | None] = mapped_column(Integer)

    signed_at: Mapped[date] = mapped_column(Date, index=True)
    validity_start: Mapped[date | None] = mapped_column(Date)
    validity_end: Mapped[date | None] = mapped_column(Date)
    published_at: Mapped[date | None] = mapped_column(Date, index=True)
    updated_at_source: Mapped[date | None] = mapped_column(Date)

    cancelled: Mapped[bool] = mapped_column(Boolean, default=False)
    cancelled_at: Mapped[date | None] = mapped_column(Date)

    object_description: Mapped[str | None] = mapped_column(Text)

    agency_cnpj: Mapped[str | None] = mapped_column(String(14), index=True)
    agency_name: Mapped[str | None] = mapped_column(Text)
    unit_code: Mapped[str | None] = mapped_column(String(40))
    unit_name: Mapped[str | None] = mapped_column(Text)
    source_user: Mapped[str | None] = mapped_column(Text)

    adhesion_allowed: Mapped[bool | None] = mapped_column(Boolean)

    suspicious: Mapped[bool] = mapped_column(Boolean, default=False)
    suspicious_reasons: Mapped[list[str] | None] = mapped_column(JSONB)
    raw: Mapped[dict] = mapped_column(JSONB)


class Contrato(Base, TimestampMixin):
    """Contrato. Chave natural: numeroControlePNCP."""

    __tablename__ = "contratos"

    pncp_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    compra_pncp_id: Mapped[str | None] = mapped_column(String(80), index=True)
    ata_pncp_id: Mapped[str | None] = mapped_column(String(80), index=True)
    # True quando a ata de origem tambem esta na base (preco ja registrado la).
    derived_from_ata: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    derivation_match: Mapped[str | None] = mapped_column(String(20))
    numero_contrato: Mapped[str | None] = mapped_column(String(120))
    ano: Mapped[int | None] = mapped_column(Integer)
    processo: Mapped[str | None] = mapped_column(String(120))

    signed_at: Mapped[date] = mapped_column(Date, index=True)
    validity_start: Mapped[date | None] = mapped_column(Date)
    validity_end: Mapped[date | None] = mapped_column(Date)
    published_at: Mapped[date | None] = mapped_column(Date, index=True)
    updated_at_source: Mapped[date | None] = mapped_column(Date)

    cancelled: Mapped[bool] = mapped_column(Boolean, default=False)
    cancelled_at: Mapped[date | None] = mapped_column(Date)

    object_description: Mapped[str | None] = mapped_column(Text)

    supplier_document: Mapped[str | None] = mapped_column(String(20), index=True)
    supplier_name: Mapped[str | None] = mapped_column(Text)

    initial_value: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    global_value: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))

    agency_cnpj: Mapped[str | None] = mapped_column(String(14), index=True)
    agency_name: Mapped[str | None] = mapped_column(Text)
    unit_code: Mapped[str | None] = mapped_column(String(40))
    unit_name: Mapped[str | None] = mapped_column(Text)
    unit_uf: Mapped[str | None] = mapped_column(String(2))
    unit_city: Mapped[str | None] = mapped_column(Text)

    contract_type: Mapped[str | None] = mapped_column(Text)

    suspicious: Mapped[bool] = mapped_column(Boolean, default=False)
    suspicious_reasons: Mapped[list[str] | None] = mapped_column(JSONB)
    raw: Mapped[dict] = mapped_column(JSONB)


Index("ix_atas_signed_agency", Ata.signed_at, Ata.agency_cnpj)
Index("ix_contratos_signed_agency", Contrato.signed_at, Contrato.agency_cnpj)


class CollectionRun(Base):
    """Registro de cada execucao, para acompanhar cobertura e descartes."""

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
