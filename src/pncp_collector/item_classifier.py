"""Fase 5: classificador local de itens, treinado nos rotulos da fase 4.

Dois comandos, ambos so banco + disco (nenhuma rede):

    treinar-itens  le item_labels, exclui o gabarito humano (docs/validacao), treina
                   TF-IDF (descricao em palavras e caracteres + objeto da compra) com
                   regressao logistica, mede no holdout e no gabarito, salva o modelo
                   em `models/` (fora do git) com um JSON de metadados
    prever-itens   carrega o modelo e grava, em cada linha de `itens`, a categoria
                   prevista, a confianca (maior probabilidade) e o interesse
                   (1 - P(outros)); retomavel: so itens sem previsao deste modelo

O modelo e DERIVADO: item_labels + este codigo + versao do scikit-learn. Por isso o
binario nao vai no repositorio; qualquer maquina com o banco refaz em minutos.

Medido em 2026-11-14 (101.774 rotulos, holdout 10%): class_weight="balanced" da
P=0,83 R=0,98 em "e de interesse" (filtro do passo C); sem peso, P=0,91 R=0,96.
"""

from __future__ import annotations

import csv
import json
import logging
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import joblib
import numpy as np
from rich.console import Console
from rich.table import Table
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import f1_score, precision_recall_fscore_support
from sklearn.model_selection import train_test_split
from sklearn.pipeline import FeatureUnion, Pipeline
from sklearn.preprocessing import FunctionTransformer

from .config import Settings, settings as default_settings
from .db import DetailRepository, build_engine, build_session_factory, create_schema
from .item_rules import OUTROS, normalize_descricao
from .memory import release_heap
from .pipeline import make_progress

logger = logging.getLogger(__name__)

DATASET_TRAIN = "treino_itens"
DATASET_PREDICT = "previsao_itens"
MODEL_FILE = "item_classifier.joblib"
META_FILE = "item_classifier.json"
PREDICT_BATCH_COMPRAS = 2000
OBJETO_MAX_CHARS = 400


# --- features ------------------------------------------------------------------------
# Funcoes nomeadas (nao lambdas) para o pipeline ser serializavel com joblib.


def pick_descricao(pairs: Sequence[tuple[str, str]]) -> list[str]:
    return [p[0] for p in pairs]


def pick_objeto(pairs: Sequence[tuple[str, str]]) -> list[str]:
    return [(p[1] or "")[:OBJETO_MAX_CHARS] for p in pairs]


def build_pipeline(class_weight: str | None = "balanced", c: float = 8.0) -> Pipeline:
    """Descricao em palavras (1-2 gramas) e caracteres (3-5), objeto da compra em
    palavras; regressao logistica multinomial. `class_weight` None ou 'balanced'."""
    cw = None if class_weight in (None, "none", "") else class_weight
    features = FeatureUnion(
        [
            (
                "desc_palavras",
                Pipeline(
                    [
                        ("sel", FunctionTransformer(pick_descricao, validate=False)),
                        (
                            "tfidf",
                            TfidfVectorizer(
                                analyzer="word",
                                ngram_range=(1, 2),
                                min_df=2,
                                sublinear_tf=True,
                                strip_accents="unicode",
                            ),
                        ),
                    ]
                ),
            ),
            (
                "desc_caracteres",
                Pipeline(
                    [
                        ("sel", FunctionTransformer(pick_descricao, validate=False)),
                        (
                            "tfidf",
                            TfidfVectorizer(
                                analyzer="char_wb",
                                ngram_range=(3, 5),
                                min_df=3,
                                sublinear_tf=True,
                                strip_accents="unicode",
                                max_features=300_000,
                            ),
                        ),
                    ]
                ),
            ),
            (
                "objeto_palavras",
                Pipeline(
                    [
                        ("sel", FunctionTransformer(pick_objeto, validate=False)),
                        (
                            "tfidf",
                            TfidfVectorizer(
                                analyzer="word",
                                ngram_range=(1, 2),
                                min_df=3,
                                sublinear_tf=True,
                                strip_accents="unicode",
                                max_features=100_000,
                            ),
                        ),
                    ]
                ),
            ),
        ]
    )
    return Pipeline(
        [
            ("features", features),
            ("clf", LogisticRegression(C=c, max_iter=3000, class_weight=cw)),
        ]
    )


# --- avaliacao -------------------------------------------------------------------


def interest_metrics(y_true: Sequence[str], y_pred: Sequence[str]) -> dict[str, float]:
    """Precisao/recall da pergunta binaria 'e de interesse' (categoria != outros)."""
    t = [c != OUTROS for c in y_true]
    p = [c != OUTROS for c in y_pred]
    if not any(t):
        return {"precision": 0.0, "recall": 0.0}
    pr, rc, _, _ = precision_recall_fscore_support(t, p, average="binary", zero_division=0)
    return {"precision": round(float(pr), 4), "recall": round(float(rc), 4)}


def evaluate(y_true: Sequence[str], y_pred: Sequence[str]) -> dict[str, Any]:
    y_true = list(y_true)
    y_pred = list(y_pred)
    labels = sorted(set(y_true) | set(y_pred))
    pr, rc, f1, sup = precision_recall_fscore_support(
        y_true, y_pred, labels=labels, zero_division=0
    )
    per_class = {
        label: {
            "precision": round(float(p), 3),
            "recall": round(float(r), 3),
            "f1": round(float(f), 3),
            "support": int(s),
        }
        for label, p, r, f, s in zip(labels, pr, rc, f1, sup)
    }
    return {
        "n": len(y_true),
        "accuracy": round(float(np.mean(np.array(y_true) == np.array(y_pred))), 4),
        "macro_f1": round(float(f1_score(y_true, y_pred, average="macro", zero_division=0)), 4),
        "interest": interest_metrics(y_true, y_pred),
        "per_class": per_class,
    }


def load_validation(path: Path) -> dict[str, str]:
    """descricao_norm -> categoria humana. Arquivo ausente = sem gabarito."""
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        return {row["descricao_norm"]: row["categoria_claude"] for row in csv.DictReader(f)}


# --- treino ------------------------------------------------------------------------


@dataclass
class TrainStats:
    model_id: str = ""
    rows_total: int = 0
    rows_excluded: int = 0
    rows_train: int = 0
    rows_holdout: int = 0
    class_weight: str | None = None
    holdout: dict[str, Any] = field(default_factory=dict)
    validation: dict[str, Any] = field(default_factory=dict)
    seconds: float = 0.0
    model_path: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "rows_total": self.rows_total,
            "rows_excluded": self.rows_excluded,
            "rows_train": self.rows_train,
            "rows_holdout": self.rows_holdout,
            "class_weight": self.class_weight,
            "holdout": self.holdout,
            "validation": self.validation,
            "seconds": round(self.seconds, 1),
            "model_path": self.model_path,
        }


class ItemClassifier:
    def __init__(
        self,
        config: Settings | None = None,
        console: Console | None = None,
        repository_factory: Callable[[], AbstractContextManager[DetailRepository]] | None = None,
        models_dir: Path | None = None,
    ) -> None:
        self.config = config or default_settings
        self.console = console or Console()
        self.repository_factory = repository_factory or self._default_repository
        self.models_dir = models_dir or Path(self.config.models_dir)
        self._session_factory = None

    @contextmanager
    def _default_repository(self) -> Iterator[DetailRepository]:
        if self._session_factory is None:
            engine = build_engine(self.config)
            create_schema(engine)
            self._session_factory = build_session_factory(engine)
        with self._session_factory() as session:
            yield DetailRepository(session)

    @property
    def model_path(self) -> Path:
        return self.models_dir / MODEL_FILE

    @property
    def meta_path(self) -> Path:
        return self.models_dir / META_FILE

    # --- treinar -----------------------------------------------------------------

    def train(self, holdout: float = 0.1, class_weight: str | None = None, seed: int = 42) -> TrainStats:
        started = time.perf_counter()
        cw = self.config.item_class_weight if class_weight is None else class_weight
        stats = TrainStats(class_weight=None if cw in (None, "none", "") else cw)
        validation = load_validation(Path(self.config.item_validation_csv))

        with self.repository_factory() as repo:
            run_id = repo.start_run(DATASET_TRAIN)
            rows = repo.training_rows()
            stats.rows_total = len(rows)
            data = [r for r in rows if r[0] not in validation]
            stats.rows_excluded = len(rows) - len(data)
            val_objeto = repo.objeto_by_descricao(list(validation)) if validation else {}

            X = [(d, o) for d, o, _ in data]
            y = [c for _, _, c in data]
            if holdout > 0:
                Xtr, Xho, ytr, yho = train_test_split(
                    X, y, test_size=holdout, random_state=seed, stratify=y
                )
            else:
                Xtr, Xho, ytr, yho = X, [], y, []
            stats.rows_train, stats.rows_holdout = len(Xtr), len(Xho)

            with self.console.status(f"Treinando com {len(Xtr)} descricoes..."):
                model = build_pipeline(cw).fit(Xtr, ytr)
            if Xho:
                stats.holdout = evaluate(yho, list(model.predict(Xho)))
            if validation:
                Xv = [(d, val_objeto.get(d, "")) for d in validation]
                yv = [validation[d] for d in validation]
                stats.validation = evaluate(yv, list(model.predict(Xv)))

            stats.model_id = datetime.now(timezone.utc).strftime("itens-%Y%m%d-%H%M%S")
            self.models_dir.mkdir(parents=True, exist_ok=True)
            joblib.dump(model, self.model_path)
            stats.model_path = str(self.model_path)
            stats.seconds = time.perf_counter() - started
            meta = stats.as_dict()
            meta["sklearn"] = _sklearn_version()
            self.meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            repo.finish_run(run_id, meta)
        return stats

    # --- prever ------------------------------------------------------------------

    def load(self) -> tuple[Pipeline, str]:
        if not self.model_path.exists() or not self.meta_path.exists():
            raise FileNotFoundError(
                f"modelo nao encontrado em {self.models_dir}; rode `treinar-itens` antes"
            )
        meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
        return joblib.load(self.model_path), meta["model_id"]

    def predict(self, refresh: bool = False, limit: int | None = None) -> dict[str, Any]:
        """Grava predicted_* em `itens`, compra a compra, em lotes. Devolve o resumo."""
        model, model_id = self.load()
        classes = list(model.named_steps["clf"].classes_)
        outros_idx = classes.index(OUTROS) if OUTROS in classes else None
        started = time.perf_counter()
        summary: dict[str, Any] = {"model_id": model_id, "items": 0, "compras": 0, "categories": {}}

        with self.repository_factory() as repo:
            run_id = repo.start_run(DATASET_PREDICT)
            compras = repo.select_compras_to_predict(model_id, refresh, limit)
            with make_progress(self.console) as progress:
                task = progress.add_task("Prevendo itens (compras)", total=len(compras), detail="")
                for start in range(0, len(compras), PREDICT_BATCH_COMPRAS):
                    chunk = compras[start : start + PREDICT_BATCH_COMPRAS]
                    items = repo.items_with_objeto(chunk)
                    if items:
                        pairs = [(normalize_descricao(d), o) for _, _, d, o in items]
                        proba = model.predict_proba(pairs)
                        now = datetime.now(timezone.utc)
                        rows = []
                        for (key, numero, _, _), p in zip(items, proba):
                            best = int(p.argmax())
                            category = classes[best]
                            interest = 1.0 - float(p[outros_idx]) if outros_idx is not None else float(p[best])
                            rows.append(
                                {
                                    "compra_key": key,
                                    "numero_item": numero,
                                    "predicted_category": category,
                                    "predicted_confidence": _dec(float(p[best])),
                                    "predicted_interest": _dec(interest),
                                    "predicted_model": model_id,
                                    "predicted_at": now,
                                }
                            )
                            summary["categories"][category] = summary["categories"].get(category, 0) + 1
                        repo.update_item_predictions(rows)
                        repo.commit()
                        summary["items"] += len(rows)
                    summary["compras"] += len(chunk)
                    release_heap()
                    interesse = sum(n for c, n in summary["categories"].items() if c != OUTROS)
                    progress.update(
                        task, advance=len(chunk), detail=f"itens {summary['items']} / de interesse {interesse}"
                    )
            summary["seconds"] = round(time.perf_counter() - started, 1)
            summary["by_category_after"] = repo.count_items_by_prediction()
            repo.finish_run(run_id, summary)
        return summary


def _dec(value: float) -> Decimal:
    return Decimal(str(round(value, 4)))


def _sklearn_version() -> str:
    import sklearn

    return sklearn.__version__


# --- saida -------------------------------------------------------------------------


def render_train_summary(console: Console, stats: TrainStats) -> None:
    table = Table(title=f"Treino do classificador de itens ({stats.model_id})")
    table.add_column("Metrica")
    table.add_column("Valor", justify="right")
    table.add_row("Rotulos em item_labels", str(stats.rows_total))
    table.add_row("Excluidos (gabarito humano)", str(stats.rows_excluded))
    table.add_row("Treino / holdout", f"{stats.rows_train} / {stats.rows_holdout}")
    table.add_row("class_weight", str(stats.class_weight))
    for name, ev in (("holdout", stats.holdout), ("gabarito humano", stats.validation)):
        if ev:
            table.add_row(f"{name}: acerto", f"{ev['accuracy']:.3f}")
            table.add_row(f"{name}: macro F1", f"{ev['macro_f1']:.3f}")
            table.add_row(
                f"{name}: interesse P / R",
                f"{ev['interest']['precision']:.3f} / {ev['interest']['recall']:.3f}",
            )
    table.add_row("Tempo", f"{stats.seconds:.0f}s")
    table.add_row("Modelo", stats.model_path)
    console.print(table)

    if stats.holdout:
        per = Table(title="Holdout por classe (precisao / recall / n)")
        per.add_column("Classe")
        per.add_column("P", justify="right")
        per.add_column("R", justify="right")
        per.add_column("n", justify="right")
        for label, m in sorted(stats.holdout["per_class"].items()):
            per.add_row(label, f"{m['precision']:.2f}", f"{m['recall']:.2f}", str(m["support"]))
        console.print(per)


def render_predict_summary(console: Console, summary: dict[str, Any]) -> None:
    table = Table(title=f"Previsao de itens ({summary['model_id']})")
    table.add_column("Metrica")
    table.add_column("Valor", justify="right")
    table.add_row("Compras percorridas", str(summary["compras"]))
    table.add_row("Itens previstos nesta execucao", str(summary["items"]))
    for category, n in sorted(summary["categories"].items(), key=lambda kv: -kv[1]):
        table.add_row(f"    {category}", str(n))
    table.add_row("Tempo", f"{summary.get('seconds', 0):.0f}s")
    console.print(table)

    after = summary.get("by_category_after") or {}
    if after:
        total = Table(title="Itens por categoria prevista (banco inteiro)")
        total.add_column("Categoria")
        total.add_column("Itens", justify="right")
        for category, n in sorted(after.items(), key=lambda kv: -kv[1]):
            total.add_row(category, str(n))
        console.print(total)


__all__ = [
    "ItemClassifier",
    "TrainStats",
    "build_pipeline",
    "evaluate",
    "interest_metrics",
    "load_validation",
    "render_predict_summary",
    "render_train_summary",
]
