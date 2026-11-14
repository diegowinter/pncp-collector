"""Fase 3: regras de decisao, cliente do endpoint de decisoes e orquestracao da triagem."""

from __future__ import annotations

import json
from contextlib import contextmanager
from decimal import Decimal

import httpx
import pytest
from rich.console import Console

from pncp_collector.config import Settings
from pncp_collector.db import TriageRow
from pncp_collector.decisions import DecisionsClient, DecisionsError, DecisionsResult
from pncp_collector.models import TriageStatus
from pncp_collector.triage import Triager
from pncp_collector.triage_rules import (
    GRUPOS,
    Clarity,
    build_questions,
    build_state,
    decide,
    suggested_groups,
)


def answers(potencial=0.1, clareza=Clarity.CLARO, conf=0.9, **grupos):
    out = {
        "clareza": {"type": "choice", "choice": clareza, "confidence": conf, "probabilities": {}},
        "potencial": {"type": "noul", "noul": potencial},
    }
    for g in GRUPOS:
        out[g] = {"type": "noul", "noul": grupos.get(g, 0.0)}
    return out


# --- regras ------------------------------------------------------------------------------


def test_perguntas_cobrem_clareza_potencial_e_todos_os_grupos():
    q = build_questions()
    assert q["clareza"]["type"] == "choice"
    assert set(q["clareza"]["criteria"]) == {"claro", "generico", "remete_ao_edital"}
    assert q["potencial"]["type"] == "noul"
    for g in GRUPOS:
        assert q[g]["type"] == "noul"
        assert q[g]["criteria"]["true"].startswith("ENTRA:")
    assert build_state("  x ") == {"objeto_da_contratacao": "x"}


def test_grupos_cobrem_as_14_categorias_de_item_sem_repetir():
    categorias = [c for meta in GRUPOS.values() for c in meta["categorias"]]
    assert len(categorias) == 14
    assert len(set(categorias)) == 14


def test_potencial_acima_do_limiar_vira_potencial_mesmo_generico():
    d = decide(answers(potencial=0.62, clareza=Clarity.GENERICO, viaturas=0.7, armamento=0.2))
    assert d.status == TriageStatus.POTENCIAL
    assert d.potential == 0.62
    assert d.clarity == Clarity.GENERICO
    assert suggested_groups(d.groups) == ["viaturas"]


def test_grupo_acima_do_limiar_vira_potencial_mesmo_com_potencial_global_baixo():
    # objeto misto: "moveis, informatica e playground" -> potencial 0.15, ti 0.59
    d = decide(answers(potencial=0.15, ti_comunicacao=0.59))
    assert d.status == TriageStatus.POTENCIAL
    assert suggested_groups(d.groups) == ["ti_comunicacao"]
    assert decide(answers(potencial=0.15, ti_comunicacao=0.59), group_threshold=0.6).status == TriageStatus.FORA_POR_ORA


def test_grupo_servicos_sozinho_nao_basta():
    # "prestacao de servicos de plantao medico": servicos 0.7, potencial 0.03
    assert decide(answers(potencial=0.03, servicos=0.7)).status == TriageStatus.FORA_POR_ORA
    # concurso publico de verdade pontua tambem na global
    assert decide(answers(potencial=0.8, servicos=0.9)).status == TriageStatus.POTENCIAL


def test_generico_ou_edital_sem_potencial_vira_indefinido():
    assert decide(answers(potencial=0.1, clareza=Clarity.GENERICO)).status == TriageStatus.INDEFINIDO
    assert (
        decide(answers(potencial=0.2, clareza=Clarity.REMETE_AO_EDITAL)).status
        == TriageStatus.INDEFINIDO
    )


def test_claro_sem_potencial_fica_fora_por_ora():
    d = decide(answers(potencial=0.03, clareza=Clarity.CLARO))
    assert d.status == TriageStatus.FORA_POR_ORA
    assert d.error is None


def test_limiar_configuravel_e_resposta_quebrada_vira_erro():
    assert decide(answers(potencial=0.3), potential_threshold=0.25).status == TriageStatus.POTENCIAL
    d = decide({"clareza": {"choice": "claro"}})
    assert d.status == TriageStatus.ERRO
    assert "resposta inesperada" in d.error


# --- cliente -----------------------------------------------------------------------------


def _response(status, body):
    return httpx.Response(status, json=body)


def test_cliente_envia_envelope_e_le_answers_e_usage():
    seen = {}

    def handler(request: httpx.Request):
        seen["auth"] = request.headers["Authorization"]
        seen["body"] = json.loads(request.content)
        return _response(
            200,
            {
                "id": "gen-1",
                "model": "typesafe/jev-1.14-20261014",
                "answers": answers(potencial=0.9),
                "usage": {"cost": 0.00002, "input_tokens": 500, "output_tokens": 80},
            },
        )

    with DecisionsClient("k", transport=httpx.MockTransport(handler), max_retries=1) as client:
        result = client.decide({"objeto_da_contratacao": "pneus"}, {"q": {}})

    assert seen["auth"] == "Bearer k"
    assert seen["body"]["model"] == "typesafe/jev-1.14"
    assert seen["body"]["state"] == {"objeto_da_contratacao": "pneus"}
    assert result.answers["potencial"]["noul"] == 0.9
    assert result.cost_usd == 0.00002
    assert result.input_tokens == 500
    assert result.model == "typesafe/jev-1.14-20261014"


def test_cliente_repete_em_429_e_desiste_em_400():
    calls = {"n": 0}

    def flaky(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return _response(429, {"error": {"message": "slow down"}})
        return _response(200, {"answers": {}, "usage": {}})

    client = DecisionsClient("k", transport=httpx.MockTransport(flaky), max_retries=3, max_backoff=0.01)
    assert client.decide({}, {}).answers == {}
    assert calls["n"] == 2

    def bad(request):
        return _response(400, {"error": {"message": "invalid questions"}})

    client = DecisionsClient("k", transport=httpx.MockTransport(bad), max_retries=3)
    with pytest.raises(DecisionsError) as exc:
        client.decide({}, {})
    assert exc.value.status_code == 400
    assert "invalid questions" in str(exc.value)


def test_cliente_exige_chave():
    with pytest.raises(ValueError):
        DecisionsClient("")


# --- orquestracao ------------------------------------------------------------------------


class FakeDecisions:
    """Resposta por objeto; excecao e levantada. Conta chamadas."""

    def __init__(self):
        self.by_objeto = {}
        self.calls = []
        self.stopped = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def stop(self):
        self.stopped = True

    def decide(self, state, questions):
        objeto = state["objeto_da_contratacao"]
        self.calls.append(objeto)
        value = self.by_objeto.get(objeto)
        if isinstance(value, Exception):
            raise value
        return DecisionsResult(
            answers=value, model="jev-test", cost_usd=0.00003, input_tokens=10, output_tokens=2,
            raw={"answers": value, "usage": {"cost": 0.00003}, "id": "gen"},
        )


class MemoryTriageRepository:
    def __init__(self):
        self.compras = {}
        self.runs = []
        self.commits = 0
        self.seeded = 0

    def add(self, key, objeto, status=TriageStatus.PENDENTE, attempts=0, source="ata"):
        self.compras[key] = {
            "objeto": objeto, "triage_status": status, "triage_attempts": attempts, "objeto_source": source
        }

    def commit(self):
        self.commits += 1

    def start_run(self, dataset):
        self.runs.append({"dataset": dataset})
        return len(self.runs)

    def finish_run(self, run_id, stats):
        self.runs[run_id - 1]["stats"] = stats

    def seed_compras_from_capas(self):
        return self.seeded

    def count_compras_by_triage(self, source=None):
        out = {}
        for c in self.compras.values():
            if source and c["objeto_source"] != source:
                continue
            out[c["triage_status"]] = out.get(c["triage_status"], 0) + 1
        return out

    def select_compras_for_triage(self, statuses, *, max_attempts, limit=None, source=None):
        rows = []
        for key, c in self.compras.items():
            if c["triage_status"] not in statuses or c["objeto"] is None:
                continue
            if source and c["objeto_source"] != source:
                continue
            if (
                max_attempts is not None
                and c["triage_status"] == TriageStatus.ERRO
                and c["triage_attempts"] >= max_attempts
            ):
                continue
            rows.append(TriageRow(compra_key=key, objeto=c["objeto"], triage_attempts=c["triage_attempts"]))
        return rows[:limit] if limit else rows

    def update_triage(self, rows):
        for row in rows:
            self.compras[row["compra_key"]].update({k: v for k, v in row.items() if k != "compra_key"})


@pytest.fixture
def world():
    repo = MemoryTriageRepository()
    client = FakeDecisions()

    @contextmanager
    def repo_factory():
        yield repo

    config = Settings(
        database_url="postgresql+psycopg://x/y",
        openrouter_api_key="k",
        triage_concurrency=2,
        triage_max_attempts=2,
    )
    triager = Triager(
        config=config,
        console=Console(quiet=True),
        repository_factory=repo_factory,
        client_factory=lambda cfg: client,
    )
    return triager, repo, client


def test_triagem_grava_status_probabilidades_e_custo(world):
    triager, repo, client = world
    repo.add("c1", "aquisicao de viaturas")
    repo.add("c2", "aquisicao de medicamentos")
    repo.add("c3", "bens comuns")
    repo.add("c4", "ja triada", status=TriageStatus.POTENCIAL)
    client.by_objeto["aquisicao de viaturas"] = answers(potencial=0.9, viaturas=0.95)
    client.by_objeto["aquisicao de medicamentos"] = answers(potencial=0.02)
    client.by_objeto["bens comuns"] = answers(potencial=0.3, clareza=Clarity.GENERICO)

    stats = triager.run()

    assert sorted(client.calls) == ["aquisicao de medicamentos", "aquisicao de viaturas", "bens comuns"]
    assert repo.compras["c1"]["triage_status"] == TriageStatus.POTENCIAL
    assert repo.compras["c1"]["triage_potential"] == Decimal("0.9")
    assert repo.compras["c1"]["triage_groups"]["viaturas"] == 0.95
    assert repo.compras["c1"]["triage_model"] == "jev-test"
    assert repo.compras["c1"]["triage_raw"]["answers"]["potencial"]["noul"] == 0.9
    assert repo.compras["c1"]["triage_attempts"] == 1
    assert repo.compras["c2"]["triage_status"] == TriageStatus.FORA_POR_ORA
    assert repo.compras["c3"]["triage_status"] == TriageStatus.INDEFINIDO
    assert repo.compras["c4"]["triage_status"] == TriageStatus.POTENCIAL
    assert stats.processed == 3 and stats.calls == 3
    assert stats.cost_usd == pytest.approx(0.00009)
    assert stats.compras_after[TriageStatus.POTENCIAL] == 2
    assert repo.runs[0]["dataset"] == "triagem"
    assert repo.runs[0]["stats"]["decided"][TriageStatus.POTENCIAL] == 1


def test_erro_do_modelo_marca_erro_e_respeita_max_attempts(world):
    triager, repo, client = world
    repo.add("c1", "objeto ruim")
    repo.add("c2", "objeto esgotado", status=TriageStatus.ERRO, attempts=2)
    client.by_objeto["objeto ruim"] = DecisionsError(502, "HTTP 502: upstream")

    stats = triager.run()

    assert client.calls == ["objeto ruim"]
    assert repo.compras["c1"]["triage_status"] == TriageStatus.ERRO
    assert "HTTP 502" in repo.compras["c1"]["triage_error"]
    assert repo.compras["c1"]["triage_attempts"] == 1
    assert "triage_potential" not in repo.compras["c1"]
    assert stats.errors["HTTP 502"] == 1
    assert stats.calls == 0


def test_only_seed_e_limit(world):
    triager, repo, client = world
    repo.seeded = 7
    repo.add("c1", "a")
    repo.add("c2", "b")
    client.by_objeto["a"] = answers(potencial=0.9)
    client.by_objeto["b"] = answers(potencial=0.9)

    stats = triager.run(triage=False)
    assert stats.seeded == 7 and client.calls == []

    stats = triager.run(seed=False, limit=1)
    assert stats.seeded == 0 and len(client.calls) == 1


def test_teto_de_custo_para_a_execucao(world):
    triager, repo, client = world
    triager.config = triager.config.model_copy(update={"triage_max_cost_usd": 0.00001})
    for i in range(3):
        repo.add(f"c{i}", f"obj{i}")
        client.by_objeto[f"obj{i}"] = answers(potencial=0.9)
    # BATCH_SIZE e 100: o primeiro lote leva as 3; o teto so e checado entre lotes.
    stats = triager.run(seed=False)
    assert stats.processed == 3
    assert stats.cost_usd >= 0.00001


def test_origem_ata_tria_so_compras_vindas_de_atas(world):
    triager, repo, client = world
    repo.add("a1", "notebooks", source="ata")
    repo.add("c1", "viaturas", source="contrato")
    client.by_objeto["notebooks"] = answers(potencial=0.9)
    client.by_objeto["viaturas"] = answers(potencial=0.9)

    stats = triager.run(seed=False, source="ata")

    assert client.calls == ["notebooks"]
    assert repo.compras["c1"]["triage_status"] == TriageStatus.PENDENTE
    assert stats.compras_after == {TriageStatus.POTENCIAL: 1}
    assert repo.runs[0]["stats"]["source"] == "ata"
