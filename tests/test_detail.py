"""Orquestracao da fase 2 com cliente e repositorio falsos (sem rede, sem Postgres)."""

from contextlib import contextmanager
from datetime import date

import pytest
from rich.console import Console

from pncp_collector.client import DetailFetchError, DetailNotFound
from pncp_collector.config import Settings
from pncp_collector.detail import Detailer
from pncp_collector.db import CompraRow, DocumentRow
from pncp_collector.models import DetailStatus, ItemsStatus, ResultsStatus

CNPJ = "07954480000179"


def compra_key(seq, ano=2025):
    return f"{CNPJ}-1-{seq:06d}/{ano}"


def ata_id(seq, seq_ata=1, ano=2025):
    return f"{compra_key(seq, ano)}-{seq_ata:06d}"


def contrato_id(seq, ano=2026):
    return f"{CNPJ}-2-{seq:06d}/{ano}"


def item(n, situacao="Homologado", tem_resultado=True):
    return {
        "numeroItem": n,
        "descricao": f"item {n}",
        "situacaoCompraItem": 2,
        "situacaoCompraItemNome": situacao,
        "temResultado": tem_resultado,
        "valorUnitarioEstimado": 10.0,
    }


def resultado(n, seq=1, ordem=1, valor=9.5):
    return {
        "numeroItem": n,
        "sequencialResultado": seq,
        "ordemClassificacaoSrp": ordem,
        "valorUnitarioHomologado": valor,
        "niFornecedor": "1",
    }


ARQ_OUTROS = {"sequencialDocumento": 1, "tipoDocumentoNome": "Outros Documentos", "titulo": "ata.pdf"}
ARQ_CONTRATO = {"sequencialDocumento": 1, "tipoDocumentoNome": "Contrato", "titulo": "c.pdf"}
ARQ_EMPENHO = {"sequencialDocumento": 2, "tipoDocumentoNome": "Nota de Empenho", "titulo": "ne.pdf"}


# --- dubles ----------------------------------------------------------------------------


class FakeClient:
    """Respostas por chave; um valor que e excecao e levantado. Conta as chamadas."""

    def __init__(self, config=None):
        self.arquivos = {}
        self.itens = {}
        self.resultados = {}
        self.contratos = {}
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def _answer(self, table, key, default):
        value = table.get(key, default)
        if isinstance(value, Exception):
            raise value
        return value

    def get_arquivos_ata(self, ref):
        self.calls.append(("arquivos_ata", ref.key))
        return self._answer(self.arquivos, ref.key, [])

    def get_arquivos_contrato(self, ref):
        self.calls.append(("arquivos_contrato", ref.key))
        return self._answer(self.arquivos, ref.key, [])

    def get_itens(self, ref):
        self.calls.append(("itens", ref.key))
        return self._answer(self.itens, ref.key, [])

    def get_resultados(self, ref, numero_item):
        self.calls.append(("resultados", ref.key, numero_item))
        return self._answer(self.resultados, (ref.key, numero_item), [])

    def get_contrato(self, ref):
        self.calls.append(("contrato", ref.key))
        return self._answer(self.contratos, ref.key, DetailNotFound(ref.key, 404, "404"))

    def count(self, endpoint):
        return sum(1 for c in self.calls if c[0] == endpoint)


class MemoryRepository:
    """Mesmas regras de selecao do DetailRepository, sobre dicts."""

    def __init__(self):
        self.docs = {"ata": {}, "contrato": {}}
        self.compras = {}
        self.itens = {}
        self.resultados = {}
        self.arquivos = {}
        self.runs = []
        self.commits = 0

    # montagem de cenarios
    def add_ata(self, id, compra=None, status=DetailStatus.PENDENTE, **extra):
        self.docs["ata"][id] = {
            "numero_controle_pncp_compra": compra if compra is not None else id.rsplit("-", 1)[0],
            "detail_status": status,
            "detail_attempts": 0,
            "compra_key": None,
            "detailed_at": None,
            "data_assinatura": date(2026, 1, 1),
            **extra,
        }

    def add_contrato(self, id, compra, status=DetailStatus.PENDENTE, **extra):
        self.docs["contrato"][id] = {
            "numero_controle_pncp_compra": compra,
            "detail_status": status,
            "detail_attempts": 0,
            "compra_key": None,
            "detailed_at": None,
            "data_assinatura": date(2026, 1, 1),
            **extra,
        }

    def status(self, tipo, id):
        return self.docs[tipo][id]["detail_status"]

    # protocolo
    def commit(self):
        self.commits += 1

    def start_run(self, dataset):
        self.runs.append({"dataset": dataset})
        return len(self.runs)

    def finish_run(self, run_id, stats):
        self.runs[run_id - 1]["stats"] = stats

    def _doc_row(self, tipo, id):
        d = self.docs[tipo][id]
        return DocumentRow(
            tipo=tipo,
            id=id,
            numero_controle_pncp_compra=d["numero_controle_pncp_compra"],
            detail_status=d["detail_status"],
            detail_attempts=d["detail_attempts"],
            compra_key=d["compra_key"],
            detailed_at=d["detailed_at"],
        )

    def select_documents(self, tipo, statuses, *, max_attempts, limit=None, cnpj=None):
        rows = []
        for id, d in self.docs[tipo].items():
            if d["detail_status"] not in statuses:
                continue
            if d["detail_status"] == DetailStatus.PENDENTE and d["compra_key"] and d["detailed_at"]:
                continue
            if d["detail_status"] == DetailStatus.ERRO and d["detail_attempts"] >= max_attempts:
                continue
            rows.append(self._doc_row(tipo, id))
        return rows[:limit] if limit else rows

    def update_documents(self, tipo, rows):
        for row in rows:
            self.docs[tipo][row["id"]].update({k: v for k, v in row.items() if k != "id"})

    def _pending_docs_of(self, key):
        return any(
            d["compra_key"] == key and d["detail_status"] == DetailStatus.PENDENTE
            for docs in self.docs.values()
            for d in docs.values()
        )

    def documents_awaiting_close(self):
        out = []
        for tipo, docs in self.docs.items():
            for id, d in docs.items():
                if d["detail_status"] == DetailStatus.PENDENTE and d["compra_key"] and d["detailed_at"]:
                    c = self.compras.get(d["compra_key"])
                    out.append((self._doc_row(tipo, id), self._compra_row(c) if c else None))
        return out

    def count_documents_by_status(self):
        out = {}
        for tipo, docs in self.docs.items():
            counts = {}
            for d in docs.values():
                counts[d["detail_status"]] = counts.get(d["detail_status"], 0) + 1
            out[tipo] = counts
        return out

    def ensure_compras(self, rows):
        for row in rows:
            self.compras.setdefault(
                row["compra_key"],
                {
                    **row,
                    "items_status": ItemsStatus.PENDENTE,
                    "items_attempts": 0,
                    "items_error": None,
                    "total_homologados": None,
                    "results_status": ResultsStatus.PENDENTE,
                    "results_attempts": 0,
                },
            )

    def reset_compras(self, keys):
        for key in keys:
            self.compras[key]["items_status"] = ItemsStatus.PENDENTE
            self.compras[key]["results_status"] = ResultsStatus.PENDENTE

    def _compra_row(self, c):
        return CompraRow(
            compra_key=c["compra_key"],
            items_status=c["items_status"],
            items_attempts=c["items_attempts"],
            items_error=c["items_error"],
            total_homologados=c["total_homologados"],
            results_status=c["results_status"],
            results_attempts=c["results_attempts"],
        )

    def select_compras_for_items(self, statuses, *, max_attempts, limit=None):
        rows = [
            self._compra_row(c)
            for key, c in self.compras.items()
            if c["items_status"] in statuses
            and not (c["items_status"] == ItemsStatus.ERRO and c["items_attempts"] >= max_attempts)
            and self._pending_docs_of(key)
        ]
        return rows[:limit] if limit else rows

    def select_compras_for_results(self, *, max_attempts, limit=None):
        rows = [
            self._compra_row(c)
            for key, c in self.compras.items()
            if c["items_status"] == ItemsStatus.OK
            and c["results_status"] in (ResultsStatus.PENDENTE, ResultsStatus.PARCIAL, ResultsStatus.ERRO)
            and not (c["results_status"] == ResultsStatus.ERRO and c["results_attempts"] >= max_attempts)
            and self._pending_docs_of(key)
        ]
        return rows[:limit] if limit else rows

    def update_compra(self, compra_key, **values):
        self.compras[compra_key].update(values)

    def count_compras_by_status(self):
        return {"items": {}, "results": {}}

    def upsert_itens(self, rows):
        for r in rows:
            self.itens[(r["compra_key"], r["numero_item"])] = r
        return len(rows)

    def select_items_for_results(self, compra_key, *, refresh=False):
        done = {n for (k, n, _) in self.resultados if k == compra_key}
        return sorted(
            n
            for (k, n), r in self.itens.items()
            if k == compra_key
            and r.get("situacao_compra_item_nome") == "Homologado"
            and r.get("tem_resultado")
            and (refresh or n not in done)
        )

    def upsert_resultados(self, rows):
        for r in rows:
            self.resultados[(r["compra_key"], r["numero_item"], r["sequencial_resultado"])] = r
        return len(rows)

    def upsert_arquivos(self, rows):
        for r in rows:
            self.arquivos[(r["documento_tipo"], r["documento_id"], r["sequencial_documento"])] = r
        return len(rows)


@pytest.fixture
def world():
    repo = MemoryRepository()
    client = FakeClient()

    @contextmanager
    def repo_factory():
        yield repo

    config = Settings(database_url="postgresql+psycopg://x/y", request_delay=0.0, detail_max_attempts=3)
    detailer = Detailer(
        config=config,
        console=Console(quiet=True),
        repository_factory=repo_factory,
        client_factory=lambda cfg: client,
    )
    return detailer, repo, client


# --- cenarios ----------------------------------------------------------------------------


def test_compra_compartilhada_consulta_itens_uma_vez(world):
    detailer, repo, client = world
    key = compra_key(1)
    repo.add_ata(ata_id(1, 1))
    repo.add_ata(ata_id(1, 2))
    repo.add_contrato(contrato_id(50), key)
    client.arquivos[ata_id(1, 1)] = [ARQ_OUTROS]
    client.arquivos[ata_id(1, 2)] = [ARQ_OUTROS]
    client.arquivos[contrato_id(50)] = [ARQ_EMPENHO]
    client.itens[key] = [item(1), item(2, tem_resultado=False), item(3, "Fracassado", False)]
    client.resultados[(key, 1)] = [resultado(1)]

    stats = detailer.run()

    assert client.count("itens") == 1
    assert client.count("resultados") == 1  # so o item homologado com temResultado
    assert client.count("contrato") == 0  # capa trazia a compra
    assert repo.status("ata", ata_id(1, 1)) == DetailStatus.OK
    assert repo.status("ata", ata_id(1, 2)) == DetailStatus.OK
    assert repo.status("contrato", contrato_id(50)) == DetailStatus.OK
    assert repo.docs["contrato"][contrato_id(50)]["compra_resolved_by"] == "capa"
    assert repo.compras[key]["total_itens"] == 3
    assert repo.compras[key]["total_homologados"] == 2
    assert repo.compras[key]["results_status"] == ResultsStatus.OK
    assert repo.compras[key]["url_pncp"] == f"https://pncp.gov.br/app/editais/{CNPJ}/2025/1"
    assert repo.resultados[(key, 1, 1)]["is_winner"] is True
    assert stats.items_stored == 3 and stats.results_stored == 1 and stats.files_stored == 3
    assert stats.documents_closed == 3
    assert repo.runs[0]["stats"]["documents_after"] == {"ata:ok": 2, "contrato:ok": 1}


def test_contrato_sem_compra_usa_fallback(world):
    detailer, repo, client = world
    key = compra_key(7)
    repo.add_contrato(contrato_id(60), compra=None)
    repo.add_contrato(contrato_id(61), compra="")
    client.contratos[contrato_id(60)] = {"numeroControlePncpCompra": key}
    client.arquivos[contrato_id(60)] = [ARQ_CONTRATO]
    client.itens[key] = [item(1)]
    client.resultados[(key, 1)] = [resultado(1)]

    detailer.run()

    assert client.count("contrato") == 2
    assert repo.status("contrato", contrato_id(60)) == DetailStatus.OK
    assert repo.docs["contrato"][contrato_id(60)]["compra_resolved_by"] == "fallback"
    assert repo.docs["contrato"][contrato_id(60)]["compra_key"] == key
    # fallback 404 -> sem_identificacao, sem chamada de arquivos
    assert repo.status("contrato", contrato_id(61)) == DetailStatus.SEM_IDENTIFICACAO
    assert ("arquivos_contrato", contrato_id(61)) not in client.calls


def test_sem_arquivo_alvo_nao_gasta_chamada_de_itens(world):
    detailer, repo, client = world
    key_a, key_b = compra_key(1), compra_key(2)
    repo.add_ata(ata_id(1))  # 204: nenhum arquivo
    repo.add_contrato(contrato_id(9), key_b)
    client.arquivos[contrato_id(9)] = [{"sequencialDocumento": 1, "tipoDocumentoNome": "Outros Documentos"}]
    client.itens[key_a] = [item(1)]
    client.itens[key_b] = [item(1)]

    detailer.run()

    assert repo.status("ata", ata_id(1)) == DetailStatus.SEM_ARQUIVO
    assert repo.status("contrato", contrato_id(9)) == DetailStatus.SEM_ARQUIVO
    assert client.count("itens") == 0
    assert key_a in repo.compras  # a compra fica registrada, so nao e consultada
    assert repo.docs["ata"][ata_id(1)]["detail_attempts"] == 1


def test_ata_removida_do_pncp_vira_sem_arquivo(world):
    detailer, repo, client = world
    repo.add_ata(ata_id(3))
    client.arquivos[ata_id(3)] = DetailNotFound("x", 404, "Ata nao cadastrada")
    detailer.run()
    assert repo.status("ata", ata_id(3)) == DetailStatus.SEM_ARQUIVO


def test_sem_homologado_e_revisitado_e_fecha_ok_quando_homologa(world):
    detailer, repo, client = world
    key = compra_key(4)
    repo.add_ata(ata_id(4))
    client.arquivos[ata_id(4)] = [ARQ_OUTROS]
    client.itens[key] = [item(1, "Em Andamento", False)]

    detailer.run()
    assert repo.status("ata", ata_id(4)) == DetailStatus.SEM_HOMOLOGADO
    assert repo.compras[key]["results_status"] == ResultsStatus.OK  # nada a buscar

    client.itens[key] = [item(1)]
    client.resultados[(key, 1)] = [resultado(1)]
    detailer.run()

    assert client.count("itens") == 2  # a compra foi reconsultada
    assert repo.status("ata", ata_id(4)) == DetailStatus.OK
    assert repo.docs["ata"][ata_id(4)]["detail_attempts"] == 2


def test_compra_sem_itens_vira_sem_homologado(world):
    detailer, repo, client = world
    key = compra_key(5)
    repo.add_ata(ata_id(5))
    client.arquivos[ata_id(5)] = [ARQ_OUTROS]
    client.itens[key] = DetailNotFound("x", 404, "Contratacao nao cadastrada")
    detailer.run()
    assert repo.compras[key]["items_status"] == ItemsStatus.SEM_ITENS
    assert repo.status("ata", ata_id(5)) == DetailStatus.SEM_HOMOLOGADO


def test_erro_num_item_deixa_compra_parcial_e_segunda_execucao_refaz_so_ele(world):
    detailer, repo, client = world
    key = compra_key(6)
    repo.add_ata(ata_id(6))
    client.arquivos[ata_id(6)] = [ARQ_OUTROS]
    client.itens[key] = [item(1), item(2), item(3)]
    client.resultados[(key, 1)] = [resultado(1)]
    client.resultados[(key, 2)] = DetailFetchError("x", 500, "HTTP 500")
    client.resultados[(key, 3)] = [resultado(3)]

    stats = detailer.run()
    assert repo.compras[key]["results_status"] == ResultsStatus.PARCIAL
    assert repo.status("ata", ata_id(6)) == DetailStatus.PENDENTE
    assert stats.errors["resultados"] == 1
    assert len(repo.resultados) == 2

    client.resultados[(key, 2)] = [resultado(2)]
    client.calls.clear()
    detailer.run()

    assert client.calls == [("resultados", key, 2)]  # nem arquivos, nem itens, nem os outros itens
    assert repo.compras[key]["results_status"] == ResultsStatus.OK
    assert repo.status("ata", ata_id(6)) == DetailStatus.OK


def test_erro_em_todos_os_itens_vira_erro_e_respeita_max_attempts(world):
    detailer, repo, client = world
    key = compra_key(8)
    repo.add_ata(ata_id(8))
    client.arquivos[ata_id(8)] = [ARQ_OUTROS]
    client.itens[key] = [item(1)]
    client.resultados[(key, 1)] = DetailFetchError("x", 502, "HTTP 502")

    for _ in range(4):
        detailer.run()

    assert repo.compras[key]["results_status"] == ResultsStatus.ERRO
    assert repo.compras[key]["results_attempts"] == 3  # max_attempts=3: a 4a execucao nao tentou
    assert client.count("resultados") == 3
    assert repo.status("ata", ata_id(8)) == DetailStatus.ERRO


def test_erro_em_arquivos_vira_erro_e_e_revisitado_ate_max_attempts(world):
    detailer, repo, client = world
    repo.add_ata(ata_id(9))
    client.arquivos[ata_id(9)] = DetailFetchError("x", 429, "esgotou")

    for _ in range(5):
        detailer.run()

    doc = repo.docs["ata"][ata_id(9)]
    assert doc["detail_status"] == DetailStatus.ERRO
    assert doc["detail_error"] == "esgotou"
    assert doc["detail_attempts"] == 3
    assert client.count("arquivos_ata") == 3


def test_sem_arquivo_e_ok_nao_sao_revisitados_sem_flag(world):
    detailer, repo, client = world
    key = compra_key(10)
    repo.add_ata(ata_id(10))
    repo.add_ata(ata_id(11))
    client.arquivos[ata_id(10)] = [ARQ_OUTROS]
    client.itens[key] = [item(1)]
    client.resultados[(key, 1)] = [resultado(1)]

    detailer.run()
    assert repo.status("ata", ata_id(10)) == DetailStatus.OK
    assert repo.status("ata", ata_id(11)) == DetailStatus.SEM_ARQUIVO

    client.calls.clear()
    detailer.run()
    assert client.calls == []

    client.arquivos[ata_id(11)] = [ARQ_OUTROS]
    client.itens[compra_key(11)] = [item(1)]
    client.resultados[(compra_key(11), 1)] = [resultado(1)]
    detailer.run(revisit_sem_arquivo=True)
    assert repo.status("ata", ata_id(11)) == DetailStatus.OK
    assert ("arquivos_ata", ata_id(10)) not in client.calls

    client.calls.clear()
    detailer.run(refresh=True)
    assert ("arquivos_ata", ata_id(10)) in client.calls
    assert ("itens", key) in client.calls
    assert ("resultados", key, 1) in client.calls


def test_id_invalido_e_sem_identificacao_sem_chamadas(world):
    detailer, repo, client = world
    repo.add_ata("A-1", compra="C-1")
    detailer.run()
    assert repo.status("ata", "A-1") == DetailStatus.SEM_IDENTIFICACAO
    assert client.calls == []
    # e nao volta a ser selecionada
    detailer.run()
    assert repo.docs["ata"]["A-1"]["detail_attempts"] == 1


def test_only_step_executa_so_o_pedido(world):
    detailer, repo, client = world
    key = compra_key(12)
    repo.add_ata(ata_id(12))
    client.arquivos[ata_id(12)] = [ARQ_OUTROS]
    client.itens[key] = [item(1)]
    client.resultados[(key, 1)] = [resultado(1)]

    detailer.run(steps="A")
    assert client.count("itens") == 0
    assert repo.status("ata", ata_id(12)) == DetailStatus.PENDENTE
    detailer.run(steps="BC")
    assert client.count("itens") == 1 and client.count("resultados") == 1
    assert repo.status("ata", ata_id(12)) == DetailStatus.PENDENTE
    detailer.run(steps="D")
    assert repo.status("ata", ata_id(12)) == DetailStatus.OK


def test_concurrency_mantem_o_resultado(world):
    detailer, repo, client = world
    detailer.config = detailer.config.model_copy(update={"detail_concurrency": 4})
    key = compra_key(13)
    for seq_ata in range(1, 6):
        repo.add_ata(ata_id(13, seq_ata))
        client.arquivos[ata_id(13, seq_ata)] = [ARQ_OUTROS]
    client.itens[key] = [item(n) for n in range(1, 8)]
    for n in range(1, 8):
        client.resultados[(key, n)] = [resultado(n), resultado(n, seq=2, ordem=2, valor=20)]

    stats = detailer.run()

    assert client.count("itens") == 1
    assert client.count("resultados") == 7
    assert stats.results_stored == 14
    assert all(repo.status("ata", ata_id(13, s)) == DetailStatus.OK for s in range(1, 6))
    assert sum(1 for r in repo.resultados.values() if r["is_winner"]) == 7
