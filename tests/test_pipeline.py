"""Pipeline end-to-end com API e banco falsos (sem rede, sem Postgres)."""

from datetime import date

import pytest

from pncp_collector.config import Settings
from pncp_collector.filters import DiscardReason
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
        def __init__(self, config):
            pass

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
    days = sorted(pages)
    return collector._run(
        dataset=dataset,
        days=days,
        today=TODAY,
        fetch=fetch,
        normalize=normalize,
        model=model,
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
    assert {row["pncp_id"] for row in collector.store["atas"]} == {"A-1", "A-2", "A-3"}


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

    rows = {row["pncp_id"]: row for row in collector.store["contratos"]}
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
    assert [row["pncp_id"] for row in collector.store["contratos"]] == ["K-3"]


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
        def __init__(self, config):
            pass

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
        seen=set(),
    )

    assert stats.failed_days == ["2026-09-01"]
    assert stats.kept == 1
