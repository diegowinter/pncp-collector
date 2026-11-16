"""Fase 5: pipeline do classificador, metricas e orquestracao de treino/previsao."""

from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal

import pytest
from rich.console import Console

from pncp_collector.config import Settings
from pncp_collector.item_classifier import (
    ItemClassifier,
    build_pipeline,
    evaluate,
    interest_metrics,
    load_validation,
)
from pncp_collector.item_rules import normalize_descricao

POSITIVOS = {
    "viatura": [
        "veiculo tipo suv zero km",
        "picape cabine dupla 4x4 nova",
        "motocicleta 300cc zero km",
        "van 16 lugares zero quilometro",
        "sedan 1.6 flex novo",
    ],
    "equip_ti": [
        "notebook core i5 8gb ssd",
        "computador desktop completo",
        "tablet 10 polegadas android",
        "impressora laser monocromatica",
        "scanner de mesa a4",
    ],
    "protecao_balistica": [
        "colete balistico nivel iii-a",
        "capacete balistico tatico",
        "escudo balistico nivel iii",
        "colete a prova de balas nivel ii",
        "capacete operacional policial",
    ],
}
NEGATIVOS = [
    "pneu veiculo automotivo",
    "toner para impressora hp",
    "mouse optico usb",
    "colete refletivo laranja",
    "cola quente bastao",
    "arroz tipo 1 5kg",
    "cimento portland 50kg",
    "papel a4 500 folhas",
    "cadeira escritorio giratoria",
    "caneta esferografica azul",
    "oleo lubrificante 1l",
    "capacete de obra classe b",
]


def dataset():
    rows = []
    for cat, descs in POSITIVOS.items():
        for d in descs:
            for k in range(4):  # repete com variacao para ter massa minima
                rows.append((f"{d} lote {k}", "aquisicao de equipamentos", cat))
    for d in NEGATIVOS:
        for k in range(4):
            rows.append((f"{d} lote {k}", "material de consumo", "outros"))
    return rows


def test_pipeline_aprende_e_separa_categorias():
    rows = dataset()
    model = build_pipeline("balanced").fit([(d, o) for d, o, _ in rows], [c for _, _, c in rows])
    pred = model.predict(
        [
            ("notebook core i7 16gb", "equipamentos"),
            ("veiculo tipo suv novo", "frota"),
            ("colete balistico nivel iii", "seguranca"),
            ("arroz tipo 1", "merenda"),
        ]
    )
    assert list(pred) == ["equip_ti", "viatura", "protecao_balistica", "outros"]
    proba = model.predict_proba([("arroz tipo 1", "merenda")])[0]
    assert proba.sum() == pytest.approx(1.0)


def test_pipeline_sem_peso_aceita_none_em_texto():
    assert build_pipeline("none").named_steps["clf"].class_weight is None
    assert build_pipeline(None).named_steps["clf"].class_weight is None
    assert build_pipeline("balanced").named_steps["clf"].class_weight == "balanced"


def test_metricas_de_interesse_e_avaliacao():
    y = ["outros", "viatura", "equip_ti", "outros"]
    p = ["outros", "viatura", "outros", "viatura"]
    assert interest_metrics(y, p) == {"precision": 0.5, "recall": 0.5}
    ev = evaluate(y, p)
    assert ev["n"] == 4 and ev["accuracy"] == 0.5
    assert ev["per_class"]["viatura"]["recall"] == 1.0
    assert interest_metrics(["outros"], ["outros"]) == {"precision": 0.0, "recall": 0.0}


def test_gabarito_ausente_e_vazio(tmp_path):
    assert load_validation(tmp_path / "nao_existe.csv") == {}
    f = tmp_path / "v.csv"
    f.write_text(
        "n,fatia,descricao_norm,categoria_claude,obs_claude\n"
        "1,peneira,colete balistico,protecao_balistica,\n",
        encoding="utf-8",
    )
    assert load_validation(f) == {"colete balistico": "protecao_balistica"}


class MemoryRepo:
    def __init__(self, rows):
        self.rows = rows  # (descricao_norm, objeto, categoria)
        self.itens = {}  # (compra, n) -> dict
        self.compras_objeto = {}
        self.runs = []
        self.commits = 0

    def commit(self):
        self.commits += 1

    def start_run(self, dataset):
        self.runs.append({"dataset": dataset})
        return len(self.runs)

    def finish_run(self, run_id, stats):
        self.runs[run_id - 1]["stats"] = stats

    def training_rows(self):
        return self.rows

    def objeto_by_descricao(self, descricoes):
        return {d: "contexto" for d in descricoes}

    def select_compras_to_predict(self, model_id, refresh, limit):
        keys = sorted(
            {k for (k, _), it in self.itens.items() if refresh or it.get("predicted_model") != model_id}
        )
        return keys[:limit] if limit else keys

    def items_with_objeto(self, compra_keys):
        return [
            (k, n, it["descricao"], self.compras_objeto.get(k, ""))
            for (k, n), it in self.itens.items()
            if k in compra_keys
        ]

    def update_item_predictions(self, rows):
        for r in rows:
            self.itens[(r["compra_key"], r["numero_item"])].update(r)

    def count_items_by_prediction(self):
        out = {}
        for it in self.itens.values():
            c = it.get("predicted_category") or "sem_previsao"
            out[c] = out.get(c, 0) + 1
        return out


@pytest.fixture
def world(tmp_path):
    rows = dataset()
    repo = MemoryRepo(rows)

    @contextmanager
    def factory():
        yield repo

    gabarito = tmp_path / "gabarito.csv"
    gabarito.write_text(
        "n,fatia,descricao_norm,categoria_claude,obs_claude\n"
        f"1,peneira,{rows[0][0]},{rows[0][2]},\n",
        encoding="utf-8",
    )
    config = Settings(
        database_url="postgresql+psycopg://x/y",
        models_dir=str(tmp_path / "models"),
        item_validation_csv=str(gabarito),
        item_class_weight="balanced",
    )
    clf = ItemClassifier(config, Console(quiet=True), repository_factory=factory)
    return repo, clf, tmp_path


def test_treino_exclui_gabarito_salva_modelo_e_registra_metricas(world):
    repo, clf, tmp_path = world
    stats = clf.train(holdout=0.2)
    assert stats.rows_total == len(repo.rows)
    assert stats.rows_excluded == 1
    assert stats.rows_train + stats.rows_holdout == stats.rows_total - 1
    assert stats.holdout["accuracy"] > 0.8
    assert stats.validation["n"] == 1 and stats.validation["accuracy"] == 1.0
    assert (tmp_path / "models" / "item_classifier.joblib").exists()
    assert (tmp_path / "models" / "item_classifier.json").exists()
    assert stats.model_id.startswith("itens-")
    assert repo.runs[0]["dataset"] == "treino_itens"
    assert repo.runs[0]["stats"]["model_id"] == stats.model_id


def test_previsao_grava_categoria_confianca_interesse_e_retoma(world):
    repo, clf, _ = world
    repo.compras_objeto = {"c1": "aquisicao de viaturas", "c2": "merenda escolar"}
    repo.itens = {
        ("c1", 1): {"descricao": "VEICULO   tipo SUV novo"},
        ("c1", 2): {"descricao": "Pneu veiculo automotivo"},
        ("c2", 1): {"descricao": "Arroz tipo 1 5kg"},
    }
    with pytest.raises(FileNotFoundError):
        clf.predict()
    clf.train(holdout=0.0)

    summary = clf.predict()

    suv = repo.itens[("c1", 1)]
    assert suv["predicted_category"] == "viatura"
    assert isinstance(suv["predicted_confidence"], Decimal)
    assert suv["predicted_interest"] > Decimal("0.5")
    assert repo.itens[("c2", 1)]["predicted_category"] == "outros"
    assert repo.itens[("c2", 1)]["predicted_interest"] < Decimal("0.5")
    assert summary["items"] == 3 and summary["compras"] == 2
    assert repo.runs[-1]["dataset"] == "previsao_itens"
    # retomada: nada a fazer com o mesmo modelo
    assert clf.predict()["items"] == 0
    assert clf.predict(refresh=True)["items"] == 3


def test_normalizacao_usada_na_previsao_bate_com_a_das_etiquetas():
    assert normalize_descricao("VEICULO   tipo SUV novo") == "veiculo tipo suv novo"
