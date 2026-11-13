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
from .ids import parse_compra_key, url_publica_compra
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
    TriageStatus,
)


# Parametros de conexao do psycopg/libpq. Keepalive TCP: A varredura passa minutos sem tocar o
# banco enquanto espera a API, e um Postgres remoto atras de NAT/firewall/
# balanceador derruba conexao ociosa sem avisar: o cliente so descobre na
# proxima escrita, com "could not receive data from server: Connection timed
# out". Com keepalive o socket e sondado a cada 60 s e declarado morto em
# ~50 s de silencio, e o mapeamento do NAT nao expira.
KEEPALIVE_ARGS = {
    "keepalives": 1,
    "keepalives_idle": 60,
    "keepalives_interval": 10,
    "keepalives_count": 5,
    "connect_timeout": 30,
    # Sem prepared statements. O psycopg prepara toda query repetida 5 vezes e
    # guarda ate 100 delas, com o texto inteiro como chave; os inserts de ate
    # 500 linhas x ~40 colunas da fase 1 tem ~1 MB de texto cada e mudam de
    # forma a cada pagina (numero de linhas), entao o cache so gasta memoria
    # aqui e no servidor, sem reaproveitar nada.
    "prepare_threshold": None,
}

# Conexao e devolvida ao pool e recriada depois disto (segundos), antes que um
# limite de idade do lado do servidor a derrube no meio de uma transacao.
POOL_RECYCLE_SECONDS = 1800


def build_engine(config: Settings | None = None) -> Engine:
    config = config or default_settings
    return create_engine(
        config.database_url,
        pool_pre_ping=True,
        pool_recycle=POOL_RECYCLE_SECONDS,
        connect_args=dict(KEEPALIVE_ARGS),
        future=True,
    )


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
    """Sem commit: quem chama decide a transacao."""
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
    """Sem commit: quem chama grava junto com os registros da pagina."""
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
    # .scalars() evita montar um Row por linha: com meio milhao de atas isso
    # pesa na caixa ARM. DISTINCT no banco: muitas atas apontam para a mesma compra.
    ata_ids = set(session.scalars(select(Ata.numero_controle_pncp_ata)))
    compra_ids = set(
        session.scalars(
            select(Ata.numero_controle_pncp_compra)
            .where(Ata.numero_controle_pncp_compra.is_not(None))
            .distinct()
        )
    )
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


@dataclass
class TriageRow:
    compra_key: str
    objeto: str
    triage_attempts: int


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
        triage_statuses: Sequence[str] | None = None,
    ) -> list[DocumentRow]:
        """Capas a passar pelo passo A, mais recentes primeiro.

        Uma capa pendente que ja passou pelo passo A (compra_key e detailed_at
        preenchidos) esta esperando a compra fechar, nao o passo A: fica de fora.
        Em erro, so ate max_attempts (None = sem limite).

        Com `triage_statuses`, so capas cuja compra (pelo numeroControlePNCPCompra
        da capa) esta nesses estados da fase 3. Capa sem compra na capa (contrato
        que depende do fallback) fica de fora do recorte.
        """
        model = DOCUMENT_MODELS[tipo]
        query = select(model).where(model.detail_status.in_(list(statuses)))
        if triage_statuses:
            triaged = (
                select(Compra.compra_key)
                .where(
                    Compra.compra_key == model.numero_controle_pncp_compra,
                    Compra.triage_status.in_(list(triage_statuses)),
                )
                .exists()
            )
            query = query.where(triaged)
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

    # --- fase 3 (triagem por objeto) --------------------------------------------------

    SEED_SOURCES = (
        # (tipo, coluna da chave da compra, coluna do objeto). Ata primeiro: o
        # objeto da ata vence quando a compra tambem tem contrato.
        ("ata", Ata.numero_controle_pncp_compra, Ata.objeto_contratacao),
        ("contrato", Contrato.numero_controle_pncp_compra, Contrato.objeto_contrato),
    )

    def seed_compras_from_capas(self, batch_size: int = 1000) -> int:
        """Cria a compra a partir da capa, com preenchimento parcial: so a chave,
        o que se deriva dela e o objeto. Compra ja existente (da fase 2) ganha o
        objeto se ainda nao tiver; nada mais e tocado. Devolve quantas compras
        foram criadas ou receberam objeto, contado pelo banco: o rowcount de um
        INSERT em lotes (insertmanyvalues) nao e confiavel."""
        before = self._count_with_objeto()
        for source, key_column, objeto_column in self.SEED_SOURCES:
            query = (
                select(key_column, func.min(objeto_column))
                .where(key_column.is_not(None), objeto_column.is_not(None))
                .group_by(key_column)
            )
            batch: list[dict[str, Any]] = []
            for key, objeto in self.session.execute(query).yield_per(batch_size):
                ref = parse_compra_key(key)
                if ref is None:
                    continue
                batch.append(
                    {
                        "compra_key": ref.key,
                        "cnpj": ref.cnpj,
                        "ano": ref.ano,
                        "sequencial": ref.sequencial,
                        "url_pncp": url_publica_compra(ref),
                        "objeto": objeto,
                        "objeto_source": source,
                    }
                )
                if len(batch) >= batch_size:
                    self._seed_batch(batch)
                    batch = []
            self._seed_batch(batch)
            self.session.commit()
        return self._count_with_objeto() - before

    def _count_with_objeto(self) -> int:
        return (
            self.session.scalar(
                select(func.count()).select_from(Compra).where(Compra.objeto.is_not(None))
            )
            or 0
        )

    def _seed_batch(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        statement = insert(Compra).values(rows)
        statement = statement.on_conflict_do_update(
            index_elements=["compra_key"],
            set_={
                "objeto": func.coalesce(Compra.objeto, statement.excluded.objeto),
                "objeto_source": func.coalesce(
                    Compra.objeto_source, statement.excluded.objeto_source
                ),
            },
            where=Compra.objeto.is_(None),
        )
        self.session.execute(statement)

    def select_compras_for_triage(
        self,
        statuses: Sequence[str],
        *,
        max_attempts: int | None,
        limit: int | None = None,
    ) -> list["TriageRow"]:
        query = select(Compra.compra_key, Compra.objeto, Compra.triage_attempts).where(
            Compra.triage_status.in_(list(statuses)), Compra.objeto.is_not(None)
        )
        if max_attempts is not None:
            query = query.where(
                ~(
                    (Compra.triage_status == TriageStatus.ERRO)
                    & (Compra.triage_attempts >= max_attempts)
                )
            )
        query = query.order_by(Compra.ano.desc(), Compra.compra_key)
        if limit:
            query = query.limit(limit)
        return [
            TriageRow(compra_key=key, objeto=objeto, triage_attempts=attempts or 0)
            for key, objeto, attempts in self.session.execute(query)
        ]

    def update_triage(self, rows: Sequence[dict[str, Any]]) -> None:
        """Cada linha traz compra_key mais as colunas triage_* a gravar."""
        for row in rows:
            values = {k: v for k, v in row.items() if k != "compra_key"}
            self.session.execute(
                update(Compra).where(Compra.compra_key == row["compra_key"]).values(**values)
            )

    def count_compras_by_triage(self) -> dict[str, int]:
        rows = self.session.execute(
            select(Compra.triage_status, func.count()).group_by(Compra.triage_status)
        )
        return {status: count for status, count in rows}

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
