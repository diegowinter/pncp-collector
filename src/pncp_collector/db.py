"""Conexao com o Postgres e escrita em lote (upsert)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import Engine, bindparam, case, create_engine, delete, func, select, text, update
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
    ItemLabel,
    ItemResultado,
    ItemsStatus,
    LabelStatus,
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


# Limite do protocolo do Postgres: 65535 parametros por comando. Uma compra com
# 5.551 itens (25 colunas cada) estourou isso em 2026-11-14; o INSERT vai em lotes.
MAX_BIND_PARAMS = 60000


def upsert(session: Session, model: type[Base], rows: Sequence[dict[str, Any]]) -> int:
    """Insere ou atualiza pela chave primaria. Reexecucoes ficam idempotentes.

    No conflito so as colunas que vieram nas linhas sao atualizadas: as demais
    (o estado da fase 2 numa capa, por exemplo) ficam como estao, em vez de
    voltar ao default. Linhas demais para um comando so vao em varios.
    """
    if not rows:
        return 0

    primary_key = [column.name for column in model.__table__.primary_key.columns]
    preserved = set(primary_key) | {"collected_at", "updated_at"}
    sent = set().union(*(row.keys() for row in rows))
    updatable_names = [
        column.name
        for column in model.__table__.columns
        if column.name in sent and column.name not in preserved
    ]
    batch_size = max(1, MAX_BIND_PARAMS // max(1, len(sent)))

    for start in range(0, len(rows), batch_size):
        statement = insert(model).values(list(rows[start : start + batch_size]))
        updatable = {name: statement.excluded[name] for name in updatable_names}
        updatable["updated_at"] = func.now()
        statement = statement.on_conflict_do_update(index_elements=primary_key, set_=updatable)
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


@dataclass
class ItemLabelRow:
    """Uma descricao distinta a rotular (fase 4), com o contexto do item exemplo."""

    descricao_norm: str
    descricao: str
    objeto: str | None
    unidade_medida: str | None
    valor_unitario_estimado: Any
    material_ou_servico_nome: str | None
    label_attempts: int


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
        min_interest: float | None = None,
    ) -> list[DocumentRow]:
        """Capas a passar pelo passo A, mais recentes primeiro.

        Uma capa pendente que ja passou pelo passo A (compra_key e detailed_at
        preenchidos) esta esperando a compra fechar, nao o passo A: fica de fora.
        Em erro, so ate max_attempts (None = sem limite).

        Com `triage_statuses`, so capas cuja compra (pelo numeroControlePNCPCompra
        da capa) esta nesses estados da fase 3. Capa sem compra na capa (contrato
        que depende do fallback) fica de fora do recorte.

        Com `min_interest`, so capas cuja compra tem item de interesse (o mesmo
        recorte do passo C; ver `_interest_item`).
        """
        model = DOCUMENT_MODELS[tipo]
        query = select(model).where(model.detail_status.in_(list(statuses)))
        if min_interest is not None:
            query = query.where(
                model.numero_controle_pncp_compra.in_(
                    select(Item.compra_key).where(*self._interest_item(min_interest))
                )
            )
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
        self,
        *,
        max_attempts: int | None,
        limit: int | None = None,
        min_interest: float | None = None,
        refresh: bool = False,
    ) -> list[CompraRow]:
        """Compras a passar pelo passo C.

        Sem `min_interest`: resultado pendente e documento pendente (com arquivo alvo).

        Com `min_interest` (filtro do classificador): compras com item de interesse
        ainda sem resultado buscado. Nao depende de documento nem de
        `results_status`, so do item: compra ja fechada volta sozinha se uma nova
        previsao marcar outro item dela.
        """
        if min_interest is None:
            query = select(Compra).where(
                Compra.items_status == ItemsStatus.OK,
                Compra.results_status.in_(
                    [ResultsStatus.PENDENTE, ResultsStatus.PARCIAL, ResultsStatus.ERRO]
                ),
                self._has_pending_document(),
            )
        else:
            wanted = select(Item.compra_key).where(*self._interest_item(min_interest))
            if not refresh:
                wanted = wanted.where(Item.results_fetched_at.is_(None))
            query = (
                select(Compra)
                .where(Compra.items_status == ItemsStatus.OK, Compra.compra_key.in_(wanted))
                .order_by(Compra.compra_key)
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
    def _interest_item(min_interest: float) -> tuple[Any, ...]:
        """Item que o passo C filtrado consulta: previsto como de interesse e com
        resultado para buscar (homologado com temResultado)."""
        return (
            Item.predicted_interest >= min_interest,
            Item.situacao_compra_item_nome == "Homologado",
            Item.tem_resultado.is_(True),
        )

    def count_interest_results(self, min_interest: float) -> dict[str, int]:
        """Andamento do passo C filtrado: itens de interesse buscados e a buscar."""
        fetched = Item.results_fetched_at.is_not(None)
        row = self.session.execute(
            select(
                func.count().filter(fetched),
                func.count().filter(~fetched),
                func.count(func.distinct(Item.compra_key)).filter(~fetched),
            ).where(*self._interest_item(min_interest))
        ).one()
        return {"items_fetched": row[0], "items_pending": row[1], "compras_pending": row[2]}

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
        source: str | None = None,
    ) -> list["TriageRow"]:
        """`source` = "ata" ou "contrato" restringe a compras cujo objeto veio desse
        tipo de capa (compra com ata e contrato conta como "ata": a ata vence na semeadura)."""
        query = select(Compra.compra_key, Compra.objeto, Compra.triage_attempts).where(
            Compra.triage_status.in_(list(statuses)), Compra.objeto.is_not(None)
        )
        if source:
            query = query.where(Compra.objeto_source == source)
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

    def count_compras_by_triage(self, source: str | None = None) -> dict[str, int]:
        query = select(Compra.triage_status, func.count()).group_by(Compra.triage_status)
        if source:
            query = query.where(Compra.objeto_source == source)
        return {status: count for status, count in self.session.execute(query)}

    # --- itens, resultados, arquivos ----------------------------------------------

    def upsert_itens(self, rows: Sequence[dict[str, Any]]) -> int:
        return upsert(self.session, Item, rows)

    def select_items_for_results(
        self, compra_key: str, *, refresh: bool = False, min_interest: float | None = None
    ) -> list[int]:
        """Itens homologados com resultado a buscar; sem refresh, pula os ja buscados.
        Com `min_interest`, so os previstos como de interesse."""
        query = select(Item.numero_item).where(
            Item.compra_key == compra_key,
            Item.situacao_compra_item_nome == "Homologado",
            Item.tem_resultado.is_(True),
        )
        if min_interest is not None:
            query = query.where(Item.predicted_interest >= min_interest)
        if not refresh:
            query = query.where(Item.results_fetched_at.is_(None))
        return sorted(self.session.scalars(query))

    def mark_results_fetched(
        self, compra_key: str, numeros: Sequence[int], when: datetime
    ) -> None:
        """Registra que o resultado desses itens foi buscado (mesmo voltando vazio)."""
        if not numeros:
            return
        self.session.execute(
            update(Item)
            .where(Item.compra_key == compra_key, Item.numero_item.in_(list(numeros)))
            .values(results_fetched_at=when)
        )

    def upsert_resultados(self, rows: Sequence[dict[str, Any]]) -> int:
        return upsert(self.session, ItemResultado, rows)

    def upsert_arquivos(self, rows: Sequence[dict[str, Any]]) -> int:
        return upsert(self.session, Arquivo, rows)

    # --- fase 4 (rotulagem de itens) --------------------------------------------------

    # Mesma normalizacao de item_rules.normalize_descricao. Mudou uma, muda a outra.
    DESCRICAO_NORM_SQL = r"lower(regexp_replace(btrim(i.descricao), '\s+', ' ', 'g'))"

    def seed_item_labels(self, sample_slice: str, pattern: str, limit: int | None) -> int:
        """Cria as linhas de `item_labels` de uma fatia da amostra de treino, a partir
        das descricoes distintas de `itens` que ainda nao estao na tabela:

            peneira    descricao bate na regex (todas, ou ate `limit`)
            reserva    nao bate, mas a compra e `potencial` (sorteio ate `limit`)
            aleatorio  nao bate (sorteio ate `limit`)

        O sorteio e por md5 da descricao normalizada: estavel entre execucoes.
        Devolve quantas linhas entraram."""
        if sample_slice == "peneira":
            where = f"i.descricao ~* :pattern"
        elif sample_slice == "reserva":
            where = f"NOT (i.descricao ~* :pattern) AND c.triage_status = '{TriageStatus.POTENCIAL}'"
        elif sample_slice == "aleatorio":
            where = "NOT (i.descricao ~* :pattern)"
        else:
            raise ValueError(f"fatia desconhecida: {sample_slice!r}")
        norm = self.DESCRICAO_NORM_SQL
        sql = f"""
            WITH d AS (
                SELECT {norm} AS descricao_norm,
                       min(i.descricao) AS descricao,
                       min(i.compra_key) AS compra_key,
                       count(*) AS item_count
                  FROM itens i
                  JOIN compras c ON c.compra_key = i.compra_key
                 WHERE i.descricao IS NOT NULL AND btrim(i.descricao) <> '' AND {where}
                 GROUP BY 1
            ),
            picked AS (
                SELECT d.*
                  FROM d
                 WHERE NOT EXISTS (SELECT 1 FROM item_labels l WHERE l.descricao_norm = d.descricao_norm)
                 ORDER BY md5(d.descricao_norm)
                 {"LIMIT :limit" if limit else ""}
            )
            INSERT INTO item_labels (descricao_norm, descricao, compra_key, objeto, unidade_medida,
                                     valor_unitario_estimado, material_ou_servico_nome, item_count,
                                     sample_slice, label_status, label_attempts)
            SELECT p.descricao_norm, p.descricao, p.compra_key, c.objeto, x.unidade_medida,
                   x.valor_unitario_estimado, x.material_ou_servico_nome, p.item_count,
                   :slice, '{LabelStatus.PENDENTE}', 0
              FROM picked p
              JOIN compras c ON c.compra_key = p.compra_key
              JOIN LATERAL (
                    SELECT i.unidade_medida, i.valor_unitario_estimado, i.material_ou_servico_nome
                      FROM itens i
                     WHERE i.compra_key = p.compra_key AND {norm} = p.descricao_norm
                     LIMIT 1
              ) x ON true
            ON CONFLICT (descricao_norm) DO NOTHING
        """
        params: dict[str, Any] = {"pattern": pattern, "slice": sample_slice}
        if limit:
            params["limit"] = limit
        result = self.session.execute(text(sql), params)
        self.session.commit()
        return result.rowcount or 0

    def select_item_labels(
        self,
        statuses: Sequence[str],
        *,
        max_attempts: int | None,
        limit: int | None = None,
        sample_slice: str | None = None,
    ) -> list[ItemLabelRow]:
        query = select(ItemLabel).where(ItemLabel.label_status.in_(list(statuses)))
        if sample_slice:
            query = query.where(ItemLabel.sample_slice == sample_slice)
        if max_attempts is not None:
            query = query.where(
                ~(
                    (ItemLabel.label_status == LabelStatus.ERRO)
                    & (ItemLabel.label_attempts >= max_attempts)
                )
            )
        # Peneira primeiro (alfabetico: 'aleatorio' < 'peneira' < 'reserva' nao serve),
        # e dentro da fatia em ordem estavel mas embaralhada, para que um --limit
        # pequeno seja uma amostra e nao as descricoes que comecam com digito.
        order = case(
            {"peneira": 0, "reserva": 1, "aleatorio": 2, "ativo": 3}, value=ItemLabel.sample_slice, else_=4
        )
        query = query.order_by(order, func.md5(ItemLabel.descricao_norm))
        if limit:
            query = query.limit(limit)
        return [
            ItemLabelRow(
                descricao_norm=obj.descricao_norm,
                descricao=obj.descricao,
                objeto=obj.objeto,
                unidade_medida=obj.unidade_medida,
                valor_unitario_estimado=obj.valor_unitario_estimado,
                material_ou_servico_nome=obj.material_ou_servico_nome,
                label_attempts=obj.label_attempts or 0,
            )
            for obj in self.session.scalars(query)
        ]

    def update_item_labels(self, rows: Sequence[dict[str, Any]]) -> None:
        """Cada linha traz descricao_norm mais as colunas label_* a gravar."""
        for row in rows:
            values = {k: v for k, v in row.items() if k != "descricao_norm"}
            self.session.execute(
                update(ItemLabel)
                .where(ItemLabel.descricao_norm == row["descricao_norm"])
                .values(**values)
            )

    def count_item_labels(self) -> dict[str, int]:
        """`fatia:status` -> quantidade."""
        query = select(ItemLabel.sample_slice, ItemLabel.label_status, func.count()).group_by(
            ItemLabel.sample_slice, ItemLabel.label_status
        )
        return {f"{s}:{st}": n for s, st, n in self.session.execute(query)}

    # --- fase 5 (classificador local de itens) ----------------------------------------

    def training_rows(self) -> list[tuple[str, str, str]]:
        """(descricao_norm, objeto, categoria) de todo rotulo ok em item_labels."""
        query = select(ItemLabel.descricao_norm, ItemLabel.objeto, ItemLabel.label_category).where(
            ItemLabel.label_status == LabelStatus.OK
        )
        return [(d, o or "", c) for d, o, c in self.session.execute(query)]

    def objeto_by_descricao(self, descricoes: Sequence[str]) -> dict[str, str]:
        """Objeto de UMA compra exemplo para cada descricao normalizada (contexto da
        validacao, que so tem a descricao)."""
        if not descricoes:
            return {}
        norm = self.DESCRICAO_NORM_SQL
        sql = text(
            f"""select v.d, coalesce(c.objeto, '')
                  from (select {norm} as d, min(i.compra_key) as ck
                          from itens i
                         where {norm} = any(:ds)
                         group by 1) v
                  join compras c on c.compra_key = v.ck"""
        )
        return {d: o for d, o in self.session.execute(sql, {"ds": list(descricoes)})}

    def count_items_to_predict(self, model_id: str, refresh: bool) -> int:
        query = select(func.count()).select_from(Item).where(Item.descricao.is_not(None))
        if not refresh:
            query = query.where(
                (Item.predicted_model.is_(None)) | (Item.predicted_model != model_id)
            )
        return self.session.scalar(query) or 0

    def select_compras_to_predict(self, model_id: str, refresh: bool, limit: int | None) -> list[str]:
        """Chaves das compras com algum item ainda sem previsao deste modelo."""
        query = select(Item.compra_key).where(Item.descricao.is_not(None))
        if not refresh:
            query = query.where(
                (Item.predicted_model.is_(None)) | (Item.predicted_model != model_id)
            )
        query = query.group_by(Item.compra_key).order_by(Item.compra_key)
        if limit:
            query = query.limit(limit)
        return list(self.session.scalars(query))

    def items_with_objeto(self, compra_keys: Sequence[str]) -> list[tuple[str, int, str, str]]:
        """(compra_key, numero_item, descricao, objeto) dos itens dessas compras."""
        query = (
            select(Item.compra_key, Item.numero_item, Item.descricao, Compra.objeto)
            .join(Compra, Compra.compra_key == Item.compra_key)
            .where(Item.compra_key.in_(list(compra_keys)), Item.descricao.is_not(None))
        )
        return [(k, n, d, o or "") for k, n, d, o in self.session.execute(query)]

    def update_item_predictions(self, rows: Sequence[dict[str, Any]]) -> None:
        """Cada linha: compra_key, numero_item e as colunas predicted_*."""
        if not rows:
            return
        # Tabela Core, nao a entidade ORM: UPDATE em lote (executemany) com WHERE por
        # parametro nao passa pela sincronizacao de sessao, que o ORM exigiria.
        table = Item.__table__
        self.session.execute(
            update(table)
            .where(
                table.c.compra_key == bindparam("b_compra_key"),
                table.c.numero_item == bindparam("b_numero_item"),
            )
            .values(
                predicted_category=bindparam("b_category"),
                predicted_confidence=bindparam("b_confidence"),
                predicted_interest=bindparam("b_interest"),
                predicted_model=bindparam("b_model"),
                predicted_at=bindparam("b_at"),
            ),
            [
                {
                    "b_compra_key": r["compra_key"],
                    "b_numero_item": r["numero_item"],
                    "b_category": r["predicted_category"],
                    "b_confidence": r["predicted_confidence"],
                    "b_interest": r["predicted_interest"],
                    "b_model": r["predicted_model"],
                    "b_at": r["predicted_at"],
                }
                for r in rows
            ],
        )

    def count_items_by_prediction(self) -> dict[str, int]:
        query = select(Item.predicted_category, func.count()).group_by(Item.predicted_category)
        return {(c or "sem_previsao"): n for c, n in self.session.execute(query)}
