"""Conexao com o Postgres e escrita em lote (upsert)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import Engine, create_engine, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from .config import Settings, settings as default_settings
from .models import Ata, Base, Contrato


def build_engine(config: Settings | None = None) -> Engine:
    config = config or default_settings
    return create_engine(config.database_url, pool_pre_ping=True, future=True)


def build_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def create_schema(engine: Engine) -> None:
    Base.metadata.create_all(engine)


def drop_schema(engine: Engine) -> None:
    Base.metadata.drop_all(engine)


def upsert(session: Session, model: type[Ata] | type[Contrato], rows: Sequence[dict[str, Any]]) -> int:
    """Insere ou atualiza pelo pncp_id. Reexecucoes ficam idempotentes."""
    if not rows:
        return 0

    statement = insert(model).values(list(rows))
    updatable = {
        column.name: statement.excluded[column.name]
        for column in model.__table__.columns
        if column.name not in ("pncp_id", "collected_at", "updated_at")
    }
    updatable["updated_at"] = func.now()
    statement = statement.on_conflict_do_update(
        index_elements=[model.pncp_id], set_=updatable
    )
    session.execute(statement)
    return len(rows)


def load_ata_keys(session: Session) -> tuple[set[str], set[str]]:
    """Ids de atas e das compras que ja tem ata salva.

    Servem para descartar o contrato derivado de uma ata ja coletada: o preco
    daquele item ja esta registrado pela ata.
    """
    ata_ids = {row[0] for row in session.execute(select(Ata.pncp_id))}
    compra_ids = {
        row[0]
        for row in session.execute(
            select(Ata.compra_pncp_id).where(Ata.compra_pncp_id.is_not(None))
        )
    }
    return ata_ids, compra_ids
