"""Conexao com o Postgres e escrita em lote (upsert)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import Engine, create_engine, delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from .config import Settings, settings as default_settings
from .models import (
    Arquivo,
    Ata,
    Base,
    CollectionDay,
    CollectionDayProgress,
    CollectionRun,
    Compra,
    Contrato,
    DetailStatus,
    Item,
    ItemResultado,
    ItemsStatus,
    ResultsStatus,
)


def build_engine(config: Settings | None = None) -> Engine:
    config = config or default_settings
    return create_engine(config.database_url, pool_pre_ping=True, future=True)


def build_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def create_schema(engine: Engine) -> None:
    Base.metadata.create_all(engine)


def drop_schema(engine: Engine) -> None:
    Base.metadata.drop_all(engine)


def upsert(session: Session, model: type[Base], rows: Sequence[dict[str, Any]]) -> int:
    """Insere ou atualiza pela chave primaria. Reexecucoes ficam idempotentes.

    No conflito so as colunas que vieram nas linhas sao atualizadas: as demais
    (o estado da fase 2 numa capa, por exemplo) ficam como estao, em vez de
    voltar ao default.
    """
    if not rows:
        return 0

    primary_key = [column.name for column in model.__table__.primary_key.columns]
    preserved = set(primary_key) | {"collected_at", "updated_at"}
    sent = set().union(*(row.keys() for row in rows))

    statement = insert(model).values(list(rows))
    updatable = {
        column.name: statement.excluded[column.name]
        for column in model.__table__.columns
        if column.name in sent and column.name not in preserved
    }
    updatable["updated_at"] = func.now()
    statement = statement.on_conflict_do_update(
        index_elements=primary_key, set_=updatable
    )
    session.execute(statement)
    return len(rows)



# --- checkpoint da fase 1 ------------------------------------------------------


def load_done_days(session: Session, dataset: str, reference: date) -> set[date]:
    query = select(CollectionDay.day).where(
        CollectionDay.dataset == dataset, CollectionDay.reference == reference
    )
    return set(session.scalars(query))


def mark_day_done(
    session: Session, dataset: str, reference: date, day: date, records: int
) -> None:
    """Sem commit: quem chama grava junto com os registros do dia."""
    statement = insert(CollectionDay).values(
        dataset=dataset, reference=reference, day=day, records=records
    )
    session.execute(
        statement.on_conflict_do_update(
            index_elements=["dataset", "reference", "day"],
            set_={"records": statement.excluded.records, "finished_at": func.now()},
        )
    )
    session.execute(
        delete(CollectionDayProgress).where(
            CollectionDayProgress.dataset == dataset,
            CollectionDayProgress.reference == reference,
            CollectionDayProgress.day == day,
        )
    )


def load_partial_days(session: Session, dataset: str, reference: date) -> dict[date, int]:
    """Dia em andamento -> ultima pagina gravada."""
    query = select(CollectionDayProgress.day, CollectionDayProgress.pages_done).where(
        CollectionDayProgress.dataset == dataset, CollectionDayProgress.reference == reference
    )
    return dict(session.execute(query).all())


def save_day_progress(
    session: Session, dataset: str, reference: date, day: date, pages_done: int, total_pages: int
) -> None:
    """Sem commit: vai no mesmo commit dos registros da pagina."""
    statement = insert(CollectionDayProgress).values(
        dataset=dataset,
        reference=reference,
        day=day,
        pages_done=pages_done,
        total_pages=total_pages,
    )
    session.execute(
        statement.on_conflict_do_update(
            index_elements=["dataset", "reference", "day"],
            set_={
                "pages_done": statement.excluded.pages_done,
                "total_pages": statement.excluded.total_pages,
                "updated_at": func.now(),
            },
        )
    )


def latest_reference(session: Session, datasets: Sequence[str]) -> date | None:
    return session.scalar(
        select(func.max(CollectionDay.reference)).where(CollectionDay.dataset.in_(list(datasets)))
    )


def load_ata_keys(session: Session) -> tuple[set[str], set[str]]:
    """Ids de atas e das compras que ja tem ata salva.

    Servem para descartar o contrato derivado de uma ata ja coletada: o preco
    daquele item ja esta registrado pela ata.
    """
    ata_ids = {row[0] for row in session.execute(select(Ata.numero_controle_pncp_ata))}
    compra_ids = {
        row[0]
        for row in session.execute(
            select(Ata.numero_controle_pncp_compra).where(
                Ata.numero_controle_pncp_compra.is_not(None)
            )
        )
    }
    return ata_ids, compra_ids


# --- fase 2 (detalhamento) --------------------------------------------------------

DOCUMENT_MODELS: dict[str, type[Ata] | type[Contrato]] = {"ata": Ata, "contrato": Contrato}


@dataclass
class DocumentRow:
    """O que a fase 2 precisa saber de uma capa, sem carregar o ORM inteiro."""

    tipo: str
    id: str
    numero_controle_pncp_compra: str | None
    detail_status: str
    detail_attempts: int
    compra_key: str | None
    detailed_at: datetime | None


@dataclass
class CompraRow:
    compra_key: str
    items_status: str
    items_attempts: int
    items_error: str | None
    total_homologados: int | None
    results_status: str
    results_attempts: int


def _doc_row(tipo: str, obj: Ata | Contrato) -> DocumentRow:
    return DocumentRow(
        tipo=tipo,
        id=obj.numero_controle_pncp_ata if tipo == "ata" else obj.numero_controle_pncp,
        numero_controle_pncp_compra=obj.numero_controle_pncp_compra,
        detail_status=obj.detail_status,
        detail_attempts=obj.detail_attempts or 0,
        compra_key=obj.compra_key,
        detailed_at=obj.detailed_at,
    )


def _compra_row(obj: Compra) -> CompraRow:
    return CompraRow(
        compra_key=obj.compra_key,
        items_status=obj.items_status,
        items_attempts=obj.items_attempts or 0,
        items_error=obj.items_error,
        total_homologados=obj.total_homologados,
        results_status=obj.results_status,
        results_attempts=obj.results_attempts or 0,
    )


class DetailRepository:
    """Todo o acesso ao banco da fase 2, numa sessao. Os testes trocam por memoria."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def commit(self) -> None:
        self.session.commit()

    # --- execucao ------------------------------------------------------------

    def start_run(self, dataset: str) -> int:
        run = CollectionRun(dataset=dataset)
        self.session.add(run)
        self.session.commit()
        return run.id

    def finish_run(self, run_id: int, stats: dict[str, Any]) -> None:
        run = self.session.get(CollectionRun, run_id)
        if run is not None:
            run.finished_at = datetime.now(timezone.utc)
            run.stats = stats
            self.session.commit()

    # --- documentos ----------------------------------------------------------

    def select_documents(
        self,
        tipo: str,
        statuses: Sequence[str],
        *,
        max_attempts: int | None,
        limit: int | None = None,
        cnpj: str | None = None,
    ) -> list[DocumentRow]:
        """Capas a passar pelo passo A, mais recentes primeiro.

        Uma capa pendente que ja passou pelo passo A (compra_key e detailed_at
        preenchidos) esta esperando a compra fechar, nao o passo A: fica de fora.
        Em erro, so ate max_attempts (None = sem limite).
        """
        model = DOCUMENT_MODELS[tipo]
        query = select(model).where(model.detail_status.in_(list(statuses)))
        query = query.where(
            ~(
                (model.detail_status == DetailStatus.PENDENTE)
                & model.compra_key.is_not(None)
                & model.detailed_at.is_not(None)
            )
        )
        if max_attempts is not None:
            query = query.where(
                ~(
                    (model.detail_status == DetailStatus.ERRO)
                    & (model.detail_attempts >= max_attempts)
                )
            )
        if cnpj:
            cnpj_column = model.cnpj_orgao if tipo == "ata" else model.orgao_entidade_cnpj
            query = query.where(cnpj_column == cnpj)
        query = query.order_by(model.data_assinatura.desc())
        if limit:
            query = query.limit(limit)
        return [_doc_row(tipo, obj) for obj in self.session.scalars(query)]

    def update_documents(self, tipo: str, rows: Sequence[dict[str, Any]]) -> None:
        """Cada linha traz a chave primaria em "id" mais as colunas a gravar."""
        if not rows:
            return
        model = DOCUMENT_MODELS[tipo]
        pk = model.numero_controle_pncp_ata if tipo == "ata" else model.numero_controle_pncp
        for row in rows:
            values = {k: v for k, v in row.items() if k != "id"}
            self.session.execute(update(model).where(pk == row["id"]).values(**values))

    def documents_awaiting_close(self) -> list[tuple[DocumentRow, CompraRow | None]]:
        """Documentos pendentes que ja passaram pelo passo A, com a compra deles."""
        out: list[tuple[DocumentRow, CompraRow | None]] = []
        for tipo, model in DOCUMENT_MODELS.items():
            query = (
                select(model, Compra)
                .outerjoin(Compra, Compra.compra_key == model.compra_key)
                .where(
                    model.detail_status == DetailStatus.PENDENTE,
                    model.compra_key.is_not(None),
                    model.detailed_at.is_not(None),
                )
            )
            for obj, compra in self.session.execute(query):
                out.append((_doc_row(tipo, obj), _compra_row(compra) if compra else None))
        return out

    def count_documents_by_status(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for tipo, model in DOCUMENT_MODELS.items():
            rows = self.session.execute(
                select(model.detail_status, func.count()).group_by(model.detail_status)
            )
            out[tipo] = {status: count for status, count in rows}
        return out

    # --- compras -------------------------------------------------------------

    def ensure_compras(self, rows: Sequence[dict[str, Any]]) -> None:
        """INSERT ... ON CONFLICT DO NOTHING: compra ja conhecida mantem o estado."""
        if not rows:
            return
        unique = {row["compra_key"]: row for row in rows}
        statement = insert(Compra).values(list(unique.values()))
        self.session.execute(statement.on_conflict_do_nothing(index_elements=["compra_key"]))

    def reset_compras(self, keys: Sequence[str]) -> None:
        """Volta a compra para pendente (documento revisitado: itens podem ter mudado)."""
        if not keys:
            return
        self.session.execute(
            update(Compra)
            .where(Compra.compra_key.in_(list(set(keys))))
            .values(items_status=ItemsStatus.PENDENTE, results_status=ResultsStatus.PENDENTE)
        )

    def select_compras_for_items(
        self, statuses: Sequence[str], *, max_attempts: int | None, limit: int | None = None
    ) -> list[CompraRow]:
        """Compras com pelo menos um documento pendente (isto e, com arquivo alvo)."""
        query = select(Compra).where(Compra.items_status.in_(list(statuses)))
        if max_attempts is not None:
            query = query.where(
                ~(
                    (Compra.items_status == ItemsStatus.ERRO)
                    & (Compra.items_attempts >= max_attempts)
                )
            )
        query = query.where(self._has_pending_document())
        if limit:
            query = query.limit(limit)
        return [_compra_row(obj) for obj in self.session.scalars(query)]

    def select_compras_for_results(
        self, *, max_attempts: int | None, limit: int | None = None
    ) -> list[CompraRow]:
        query = select(Compra).where(
            Compra.items_status == ItemsStatus.OK,
            Compra.results_status.in_(
                [ResultsStatus.PENDENTE, ResultsStatus.PARCIAL, ResultsStatus.ERRO]
            ),
            self._has_pending_document(),
        )
        if max_attempts is not None:
            query = query.where(
                ~(
                    (Compra.results_status == ResultsStatus.ERRO)
                    & (Compra.results_attempts >= max_attempts)
                )
            )
        if limit:
            query = query.limit(limit)
        return [_compra_row(obj) for obj in self.session.scalars(query)]

    @staticmethod
    def _has_pending_document():
        pending_ata = (
            select(Ata.numero_controle_pncp_ata)
            .where(Ata.compra_key == Compra.compra_key, Ata.detail_status == DetailStatus.PENDENTE)
            .exists()
        )
        pending_contrato = (
            select(Contrato.numero_controle_pncp)
            .where(
                Contrato.compra_key == Compra.compra_key,
                Contrato.detail_status == DetailStatus.PENDENTE,
            )
            .exists()
        )
        return pending_ata | pending_contrato

    def update_compra(self, compra_key: str, **values: Any) -> None:
        self.session.execute(
            update(Compra).where(Compra.compra_key == compra_key).values(**values)
        )

    def count_compras_by_status(self) -> dict[str, dict[str, int]]:
        items = self.session.execute(
            select(Compra.items_status, func.count()).group_by(Compra.items_status)
        )
        results = self.session.execute(
            select(Compra.results_status, func.count()).group_by(Compra.results_status)
        )
        return {
            "items": {status: count for status, count in items},
            "results": {status: count for status, count in results},
        }

    # --- itens, resultados, arquivos ----------------------------------------------

    def upsert_itens(self, rows: Sequence[dict[str, Any]]) -> int:
        return upsert(self.session, Item, rows)

    def select_items_for_results(self, compra_key: str, *, refresh: bool = False) -> list[int]:
        """Itens homologados com resultado a buscar; sem refresh, pula os ja gravados."""
        query = select(Item.numero_item).where(
            Item.compra_key == compra_key,
            Item.situacao_compra_item_nome == "Homologado",
            Item.tem_resultado.is_(True),
        )
        if not refresh:
            done = select(ItemResultado.numero_item).where(
                ItemResultado.compra_key == compra_key
            )
            query = query.where(Item.numero_item.not_in(done))
        return sorted(self.session.scalars(query))

    def upsert_resultados(self, rows: Sequence[dict[str, Any]]) -> int:
        return upsert(self.session, ItemResultado, rows)

    def upsert_arquivos(self, rows: Sequence[dict[str, Any]]) -> int:
        return upsert(self.session, Arquivo, rows)
