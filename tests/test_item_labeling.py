"""Fase 4: regras da rotulagem de itens e orquestracao."""

from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal

import pytest
from rich.console import Console

from pncp_collector.config import Settings
from pncp_collector.db import ItemLabelRow
from pncp_collector.decisions import DecisionsError, DecisionsResult
from pncp_collector.item_labeling import ItemLabeler
from pncp_collector.item_rules import (
    CATEGORIAS,
    OUTROS,
    build_item_questions,
    build_item_state,
    decide_item,
    normalize_descricao,
    sieve_regex,
)
from pncp_collector.models import LabelStatus
from pncp_collector.triage_rules import GRUPOS


def answers(choice="outros", interesse=0.1, confidence=0.8, **probs):
    probabilities = {c: 0.0 for c in CATEGORIAS}
    probabilities[choice] = confidence
    probabilities.update(probs)
    return {
        "categoria": {
            "type": "choice",
            "choice": choice,
            "confidence": confidence,
            "probabilities": probabilities,
        },
        "interesse": {"type": "noul", "noul": interesse},
    }


# --- regras ------------------------------------------------------------------------------


def test_categorias_sao_as_14_dos_grupos_mais_outros():
    das_capas = {c for meta in GRUPOS.values() for c in meta["categorias"]}
    assert set(CATEGORIAS) == das_capas | {OUTROS}
    assert list(CATEGORIAS)[-1] == OUTROS


def test_perguntas_cobrem_categoria_e_interesse():
    q = build_item_questions()
    assert q["categoria"]["type"] == "choice"
    assert set(q["categoria"]["criteria"]) == set(CATEGORIAS)
    assert q["interesse"]["type"] == "noul"


def test_estado_leva_descricao_e_contexto_opcional():
    assert build_item_state("  Pneu  ") == {"descricao_do_item": "Pneu"}
    state = build_item_state(
        "Colete balistico",
        objeto="Aquisicao de coletes",
        unidade_medida="Unidade",
        valor_unitario_estimado=Decimal("1234.5"),
        material_ou_servico_nome="Material",
    )
    assert state["valor_unitario_estimado_reais"] == "1234.50"
    assert state["objeto_da_compra"] == "Aquisicao de coletes"
    assert state["material_ou_servico"] == "Material"
    assert state["unidade"] == "Unidade"


def test_normalizacao_colapsa_espacos_e_minusculas():
    assert normalize_descricao("  PNEU   veiculo\n automotivo ") == "pneu veiculo automotivo"


def test_peneira_pega_candidatos_e_homonimos_de_proposito():
    rx = sieve_regex()
    for s in ["Pistola 9mm", "COLETE BALISTICO NIVEL IIIA", "Radio comunicador HT", "notebook i5",
              "Veiculo tipo SUV", "Drone com camera", "pistola de cola quente", "cateter calibre 20"]:
        assert rx.search(s), s
    for s in ["Arroz tipo 1", "Papel A4", "Cimento Portland", "Cadeira escritorio"]:
        assert not rx.search(s), s


def test_decisao_le_escolha_confianca_interesse_e_probabilidades():
    d = decide_item(answers("viatura", interesse=0.93, confidence=0.88, outros=0.1))
    assert d.status == LabelStatus.OK
    assert d.category == "viatura"
    assert d.confidence == 0.88
    assert d.interest == 0.93
    assert d.probabilities["viatura"] == 0.88
    assert d.probabilities["outros"] == 0.1


def test_decisao_sem_confianca_usa_probabilidade_da_escolha():
    a = answers("municao", confidence=0.7)
    del a["categoria"]["confidence"]
    assert decide_item(a).confidence == 0.7


def test_decisao_rejeita_categoria_desconhecida_e_resposta_quebrada():
    d = decide_item(answers("viaturas"))
    assert d.status == LabelStatus.ERRO
    assert "desconhecida" in d.error
    d = decide_item({"categoria": {"choice": "outros"}})
    assert d.status == LabelStatus.ERRO
    assert "resposta inesperada" in d.error


# --- orquestracao ------------------------------------------------------------------------


class FakeDecisions:
    def __init__(self):
        self.by_descricao = {}
        self.calls = []
        self.stopped = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def stop(self):
        self.stopped = True

    def decide(self, state, questions):
        descricao = state["descricao_do_item"]
        self.calls.append(state)
        value = self.by_descricao.get(descricao)
        if isinstance(value, Exception):
            raise value
        return DecisionsResult(
            answers=value, model="jev-test", cost_usd=0.00005, input_tokens=10, output_tokens=2,
            raw={"answers": value, "usage": {"cost": 0.00005}, "id": "gen"},
        )


class MemoryLabelRepository:
    def __init__(self):
        self.labels = {}
        self.runs = []
        self.commits = 0
        self.seeds = []

    def add(self, descricao, sample_slice="peneira", status=LabelStatus.PENDENTE, attempts=0, **ctx):
        self.labels[normalize_descricao(descricao)] = {
            "descricao": descricao, "sample_slice": sample_slice, "label_status": status,
            "label_attempts": attempts, "objeto": ctx.get("objeto"), "unidade_medida": ctx.get("unidade"),
            "valor_unitario_estimado": ctx.get("valor"), "material_ou_servico_nome": None,
        }

    def commit(self):
        self.commits += 1

    def start_run(self, dataset):
        self.runs.append({"dataset": dataset})
        return len(self.runs)

    def finish_run(self, run_id, stats):
        self.runs[run_id - 1]["stats"] = stats

    def count_item_labels(self):
        out = {}
        for l in self.labels.values():
            key = f"{l['sample_slice']}:{l['label_status']}"
            out[key] = out.get(key, 0) + 1
        return out

    def seed_item_labels(self, sample_slice, pattern, limit):
        self.seeds.append((sample_slice, limit))
        return 0

    def select_item_labels(self, statuses, *, max_attempts, limit=None, sample_slice=None):
        rows = []
        for key, l in sorted(self.labels.items(), key=lambda kv: (kv[1]["sample_slice"], kv[0])):
            if l["label_status"] not in statuses:
                continue
            if sample_slice and l["sample_slice"] != sample_slice:
                continue
            if (
                max_attempts is not None
                and l["label_status"] == LabelStatus.ERRO
                and l["label_attempts"] >= max_attempts
            ):
                continue
            rows.append(
                ItemLabelRow(
                    descricao_norm=key, descricao=l["descricao"], objeto=l["objeto"],
                    unidade_medida=l["unidade_medida"], valor_unitario_estimado=l["valor_unitario_estimado"],
                    material_ou_servico_nome=None, label_attempts=l["label_attempts"],
                )
            )
        return rows[:limit] if limit else rows

    def update_item_labels(self, rows):
        for row in rows:
            self.labels[row["descricao_norm"]].update({k: v for k, v in row.items() if k != "descricao_norm"})


@pytest.fixture
def world():
    repo = MemoryLabelRepository()
    client = FakeDecisions()

    @contextmanager
    def repo_factory():
        yield repo

    config = Settings(
        database_url="postgresql+psycopg://x/y", openrouter_api_key="k", triage_concurrency=2,
        triage_max_cost_usd=None,
    )
    labeler = ItemLabeler(
        config, Console(quiet=True), repository_factory=repo_factory, client_factory=lambda c: client
    )
    return repo, client, labeler


def test_rotula_pendentes_grava_categoria_e_custo(world):
    repo, client, labeler = world
    repo.add("Colete balistico nivel IIIA", objeto="Aquisicao de coletes", valor=Decimal("1500"))
    repo.add("Pistola de cola quente", sample_slice="peneira")
    repo.add("Arroz tipo 1", sample_slice="aleatorio", status=LabelStatus.OK)
    client.by_descricao["Colete balistico nivel IIIA"] = answers("protecao_balistica", 0.97, 0.95)
    client.by_descricao["Pistola de cola quente"] = answers("outros", 0.02, 0.9)

    stats = labeler.run()

    colete = repo.labels["colete balistico nivel iiia"]
    assert colete["label_status"] == LabelStatus.OK
    assert colete["label_category"] == "protecao_balistica"
    assert colete["label_confidence"] == Decimal("0.95")
    assert colete["label_interest"] == Decimal("0.97")
    assert colete["label_probabilities"]["protecao_balistica"] == 0.95
    assert colete["label_model"] == "jev-test"
    assert colete["label_attempts"] == 1
    assert colete["label_raw"]["usage"] == {"cost": 0.00005}
    assert repo.labels["pistola de cola quente"]["label_category"] == "outros"
    # o item ja rotulado nao e reenviado
    assert len(client.calls) == 2
    # o contexto vai no estado
    sent = next(c for c in client.calls if c["descricao_do_item"] == "Colete balistico nivel IIIA")
    assert sent["objeto_da_compra"] == "Aquisicao de coletes"
    assert sent["valor_unitario_estimado_reais"] == "1500.00"
    assert stats.processed == 2
    assert stats.categories == {"protecao_balistica": 1, "outros": 1}
    assert stats.cost_usd == pytest.approx(0.0001)
    assert repo.runs[0]["dataset"] == "rotulos_itens"
    assert repo.runs[0]["stats"]["calls"] == 2


def test_erro_do_modelo_fica_como_erro_e_conta_tentativa(world):
    repo, client, labeler = world
    repo.add("Radio HT")
    client.by_descricao["Radio HT"] = DecisionsError(502, "HTTP 502: bad gateway")

    stats = labeler.run()

    radio = repo.labels["radio ht"]
    assert radio["label_status"] == LabelStatus.ERRO
    assert radio["label_attempts"] == 1
    assert "502" in radio["label_error"]
    assert radio.get("label_category") is None
    assert stats.errors == {"HTTP 502": 1}
    assert stats.calls == 0


def test_semeadura_so_completa_o_que_falta_das_fatias(world):
    repo, client, labeler = world
    for i in range(3):
        repo.add(f"desc aleatoria {i}", sample_slice="aleatorio", status=LabelStatus.OK)
    repo.add("desc peneira", sample_slice="peneira", status=LabelStatus.OK)

    labeler.run(label=False, sample_reserva=5, sample_aleatorio=3)

    assert ("peneira", None) in repo.seeds
    assert ("reserva", 5) in repo.seeds
    assert all(s != "aleatorio" for s, _ in repo.seeds)


def test_teto_de_gasto_para_a_rotulagem(world):
    repo, client, labeler = world
    labeler.config = labeler.config.model_copy(update={"triage_max_cost_usd": 0.00001})
    for i in range(150):
        repo.add(f"item {i}")
        client.by_descricao[f"item {i}"] = answers()

    stats = labeler.run()

    assert stats.processed == 100  # um lote, depois o teto
    assert sum(1 for l in repo.labels.values() if l["label_status"] == LabelStatus.PENDENTE) == 50


def test_filtro_por_fatia_e_limite(world):
    repo, client, labeler = world
    repo.add("a", sample_slice="peneira")
    repo.add("b", sample_slice="reserva")
    repo.add("c", sample_slice="reserva")
    for d in "abc":
        client.by_descricao[d] = answers()

    labeler.run(seed=False, sample_slice="reserva", limit=1)

    assert len(client.calls) == 1
    assert client.calls[0]["descricao_do_item"] == "b"
