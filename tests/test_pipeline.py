"""Pipeline end-to-end com API e banco falsos (sem rede, sem Postgres)."""

from datetime import date, timedelta

import pytest

from pncp_collector.config import Settings
from pncp_collector.filters import ATA_FIELDS, CONTRATO_FIELDS, DiscardReason
from pncp_collector.models import Ata, Contrato
from pncp_collector.pipeline import Collector

TODAY = date(2026, 9, 8)


class FakeSession:
    """Sessao minima: guarda os upserts em memoria."""

    def __init__(self, store):
        self.store = store

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def add(self, obj):
        obj.id = 1

    def commit(self):
        pass

    def get(self, model, ident):
        return None


@pytest.fixture
def collector(monkeypatch):
    store: dict[str, list[dict]] = {"atas": [], "contratos": []}

    config = Settings(database_url="postgresql+psycopg://x/y", request_delay=0.0)
    instance = Collector.__new__(Collector)
    instance.config = config
    from rich.console import Console

    instance.console = Console(quiet=True)
    instance.session_factory = lambda: FakeSession(store)
    instance.store = store

    def fake_upsert(session, model, rows):
        key = "atas" if model is Ata else "contratos"
        session.store[key].extend(rows)
        return len(rows)

    monkeypatch.setattr("pncp_collector.pipeline.upsert", fake_upsert)
    return instance


def run_dataset(collector, monkeypatch, dataset, pages, ata_keys=None):
    from pncp_collector import pipeline

    class FakeClient:
        def __init__(self, config, on_page=None):
            self.on_page = on_page

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(pipeline, "PNCPClient", FakeClient)

    def fetch(client, day):
        return iter(pages.get(day, []))

    normalize = (
        pipeline.normalize_ata if dataset == "atas" else pipeline.normalize_contrato
    )
    model = Ata if dataset == "atas" else Contrato
    fields = ATA_FIELDS if dataset == "atas" else CONTRATO_FIELDS
    days = sorted(pages)
    return collector._run(
        dataset=dataset,
        days=days,
        today=TODAY,
        fetch=fetch,
        normalize=normalize,
        model=model,
        fields=fields,
        seen=set(),
        ata_keys=ata_keys,
    )


def ata_payload(pncp_id, signed="2026-06-01", compra="C-1"):
    return {
        "numeroControlePNCPAta": pncp_id,
        "numeroControlePNCPCompra": compra,
        "dataAssinatura": signed,
        "vigenciaInicio": signed,
        "vigenciaFim": "2027-01-01",
        "cancelado": False,
    }


def test_dedup_de_atas_entre_dias(collector, monkeypatch):
    pages = {
        date(2026, 9, 1): [ata_payload("A-1"), ata_payload("A-2")],
        date(2026, 9, 2): [ata_payload("A-1"), ata_payload("A-3")],
    }
    stats = run_dataset(collector, monkeypatch, "atas", pages)

    assert stats.fetched == 4
    assert stats.duplicates == 1
    assert stats.kept == 3
    stored = {row["numero_controle_pncp_ata"] for row in collector.store["atas"]}
    assert stored == {"A-1", "A-2", "A-3"}


def test_atas_antigas_sao_descartadas(collector, monkeypatch):
    pages = {
        date(2026, 9, 1): [
            ata_payload("A-1"),
            ata_payload("A-velha", signed="2023-06-16"),
        ]
    }
    stats = run_dataset(collector, monkeypatch, "atas", pages)

    assert stats.kept == 1
    assert stats.discarded[DiscardReason.OLD_SIGNATURE] == 1


DERIVADOS = {
    date(2026, 9, 1): [
        {
            "numeroControlePNCP": "K-1",
            "numeroControlePncpAta": "A-1",
            "dataAssinatura": "2026-08-01",
        },
        {
            "numeroControlePNCP": "K-2",
            "numeroControlePncpCompra": "C-9",
            "dataAssinatura": "2026-08-01",
        },
        {
            "numeroControlePNCP": "K-3",
            "numeroControlePncpCompra": "C-outra",
            "dataAssinatura": "2026-08-01",
        },
    ]
}


def test_derivado_de_ata_por_padrao_e_mantido_e_marcado(collector, monkeypatch):
    stats = run_dataset(
        collector, monkeypatch, "contratos", DERIVADOS, ata_keys=({"A-1"}, {"C-9"})
    )

    assert stats.derived_from_ata == 2
    assert stats.kept == 3
    assert stats.discarded[DiscardReason.DERIVED_FROM_ATA] == 0

    rows = {row["numero_controle_pncp"]: row for row in collector.store["contratos"]}
    assert rows["K-1"]["derived_from_ata"] and rows["K-1"]["derivation_match"] == "ata"
    assert rows["K-2"]["derived_from_ata"] and rows["K-2"]["derivation_match"] == "compra"
    assert rows["K-3"]["derived_from_ata"] is False
    assert rows["K-3"]["derivation_match"] is None


def test_derivado_de_ata_e_descartado_quando_configurado(collector, monkeypatch):
    collector.config = collector.config.model_copy(
        update={"discard_derived_contracts": True}
    )
    stats = run_dataset(
        collector, monkeypatch, "contratos", DERIVADOS, ata_keys=({"A-1"}, {"C-9"})
    )

    assert stats.derived_from_ata == 2
    assert stats.discarded[DiscardReason.DERIVED_FROM_ATA] == 2
    kept = [row["numero_controle_pncp"] for row in collector.store["contratos"]]
    assert kept == ["K-3"]


def test_marca_suspeitos_sem_descartar(collector, monkeypatch):
    pages = {
        date(2026, 9, 1): [
            {
                "numeroControlePNCPAta": "A-9",
                "dataAssinatura": "2026-05-01",
                "vigenciaInicio": "2026-04-01",
                "vigenciaFim": "2054-10-23",
            }
        ]
    }
    stats = run_dataset(collector, monkeypatch, "atas", pages)

    assert stats.kept == 1
    assert stats.suspicious == 1
    assert len(collector.store["atas"][0]["suspicious_reasons"]) == 2


def test_dia_com_falha_nao_derruba_a_varredura(collector, monkeypatch):
    from pncp_collector.client import DayFetchError
    from pncp_collector import pipeline

    class FakeClient:
        def __init__(self, config, on_page=None):
            self.on_page = on_page

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(pipeline, "PNCPClient", FakeClient)

    def fetch(client, day):
        if day == date(2026, 9, 1):
            raise DayFetchError("429 sem fim")
        return iter([ata_payload("A-1")])

    stats = collector._run(
        dataset="atas",
        days=[date(2026, 9, 1), date(2026, 9, 2)],
        today=TODAY,
        fetch=fetch,
        normalize=pipeline.normalize_ata,
        model=Ata,
        fields=ATA_FIELDS,
        seen=set(),
    )

    assert stats.failed_days == ["2026-09-01"]
    assert stats.kept == 1


# --- fase 1 em paralelo ------------------------------------------------------


def run_days(collector, monkeypatch, days, fetch, concurrency):
    from pncp_collector import pipeline

    class FakeClient:
        def __init__(self, config, on_page=None):
            self.on_page = on_page

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(pipeline, "PNCPClient", FakeClient)
    collector.config = collector.config.model_copy(
        update={"list_concurrency": concurrency}
    )
    return collector._run(
        dataset="atas",
        days=days,
        today=TODAY,
        fetch=fetch,
        normalize=pipeline.normalize_ata,
        model=Ata,
        fields=ATA_FIELDS,
        seen=set(),
    )


def test_concorrencia_preserva_a_ordem_dos_dias(collector, monkeypatch):
    """Quem responde primeiro nao muda a ordem de gravacao: o dia 1 vem antes
    do dia 5 mesmo demorando mais para chegar."""
    import time

    days = [date(2026, 9, d) for d in range(1, 6)]

    def fetch(client, day):
        # Os primeiros dias sao os mais lentos: sem ordenacao explicita, a
        # gravacao sairia invertida.
        time.sleep((6 - day.day) * 0.01)
        return iter([ata_payload(f"A-{day.day}")])

    stats = run_days(collector, monkeypatch, days, fetch, concurrency=4)

    assert stats.kept == 5
    gravados = [row["numero_controle_pncp_ata"] for row in collector.store["atas"]]
    assert gravados == ["A-1", "A-2", "A-3", "A-4", "A-5"]


def test_concorrencia_mantem_o_dedup_entre_dias(collector, monkeypatch):
    days = [date(2026, 9, d) for d in range(1, 5)]
    pages = {
        days[0]: [ata_payload("A-1"), ata_payload("A-2")],
        days[1]: [ata_payload("A-1"), ata_payload("A-3")],
        days[2]: [ata_payload("A-2"), ata_payload("A-3")],
        days[3]: [ata_payload("A-4")],
    }

    def fetch(client, day):
        return iter(pages[day])

    stats = run_days(collector, monkeypatch, days, fetch, concurrency=4)

    assert stats.kept == 4
    assert stats.duplicates == 3
    gravados = [row["numero_controle_pncp_ata"] for row in collector.store["atas"]]
    assert gravados == ["A-1", "A-2", "A-3", "A-4"]


def test_concorrencia_busca_dias_em_paralelo(collector, monkeypatch):
    """Sanidade do ganho: 6 dias de 0,1 s com 6 workers nao levam 0,6 s."""
    import time

    days = [date(2026, 9, d) for d in range(1, 7)]

    def fetch(client, day):
        time.sleep(0.1)
        return iter([ata_payload(f"A-{day.day}")])

    started = time.monotonic()
    stats = run_days(collector, monkeypatch, days, fetch, concurrency=6)
    elapsed = time.monotonic() - started

    assert stats.kept == 6
    assert elapsed < 0.4, elapsed


def test_dia_com_falha_em_paralelo_nao_derruba_os_outros(collector, monkeypatch):
    from pncp_collector.client import DayFetchError

    days = [date(2026, 9, d) for d in range(1, 5)]

    def fetch(client, day):
        if day.day in (2, 3):
            raise DayFetchError("429 sem fim")
        return iter([ata_payload(f"A-{day.day}")])

    stats = run_days(collector, monkeypatch, days, fetch, concurrency=4)

    assert stats.failed_days == ["2026-09-02", "2026-09-03"]
    assert stats.kept == 2


# --- barra auxiliar de paginas ----------------------------------------------


def test_page_tracker_soma_paginas_e_total_parcial():
    """O total cresce conforme os dias comecam: so a primeira pagina de cada
    dia revela quantas ele tem."""
    from pncp_collector.pipeline import _PageTracker

    updates = []

    class FakeProgress:
        def update(self, task_id, **kwargs):
            updates.append(kwargs)

    tracker = _PageTracker(FakeProgress(), task_id=0)
    d1, d2 = date(2026, 9, 1), date(2026, 9, 2)

    tracker(d1, 1, 3)  # dia 1 tem 3 paginas
    assert updates[-1]["completed"] == 1 and updates[-1]["total"] == 3

    tracker(d1, 2, 3)  # nao soma de novo o total do mesmo dia
    assert updates[-1]["completed"] == 2 and updates[-1]["total"] == 3

    tracker(d2, 1, 2)  # dia 2 revela mais 2 paginas
    assert updates[-1]["completed"] == 3 and updates[-1]["total"] == 5
    assert "2 dias conhecidos" in updates[-1]["detail"]


def test_page_tracker_e_thread_safe():
    import threading

    from pncp_collector.pipeline import _PageTracker

    class FakeProgress:
        def update(self, task_id, **kwargs):
            pass

    tracker = _PageTracker(FakeProgress(), task_id=0)
    days = [date(2026, 9, 1) + timedelta(days=i) for i in range(8)]

    def worker(day):
        for page in range(1, 26):
            tracker(day, page, 25)

    threads = [threading.Thread(target=worker, args=(d,)) for d in days]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert tracker._fetched == 8 * 25
    assert tracker._known == 8 * 25


def test_cliente_avisa_cada_pagina_com_o_total_do_dia():
    """O contrato entre cliente e barra: (dia, pagina, totalPaginas)."""
    import httpx

    from pncp_collector.client import PNCPClient
    from pncp_collector.config import Settings

    vistos = []
    config = Settings(
        database_url="postgresql+psycopg://x/y", request_delay=0.0, page_size=10
    )
    c = PNCPClient(config, on_page=lambda d, p, t: vistos.append((d, p, t)))
    c._client = httpx.Client(
        base_url=config.base_url,
        transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json={"data": [{"a": 1}], "totalPaginas": 3})
        ),
    )

    day = date(2026, 9, 1)
    assert len(list(c.iter_atas(day))) == 3
    assert vistos == [(day, 1, 3), (day, 2, 3), (day, 3, 3)]
