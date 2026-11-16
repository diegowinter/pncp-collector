"""upsert em lotes: uma compra com milhares de itens nao pode estourar os 65535 parametros."""

from __future__ import annotations

from pncp_collector import db
from pncp_collector.models import Item


class FakeSession:
    def __init__(self):
        self.statements = []

    def execute(self, statement):
        self.statements.append(statement)


def _rows(n):
    return [
        {"compra_key": "k", "numero_item": i, "descricao": f"item {i}", "quantidade": 1, "raw": {}}
        for i in range(n)
    ]


def test_upsert_divide_em_lotes_abaixo_do_limite_de_parametros(monkeypatch):
    monkeypatch.setattr(db, "MAX_BIND_PARAMS", 100)  # 5 colunas -> 20 linhas por comando
    session = FakeSession()
    assert db.upsert(session, Item, _rows(55)) == 55
    assert len(session.statements) == 3
    sizes = [len(s.compile().params) for s in session.statements]
    # 20 + 20 + 15 linhas, 5 colunas cada
    assert sizes == [100, 100, 75]


def test_upsert_pequeno_vai_num_comando_so_e_vazio_nao_executa():
    session = FakeSession()
    assert db.upsert(session, Item, _rows(3)) == 3
    assert len(session.statements) == 1
    assert db.upsert(session, Item, []) == 0
    assert len(session.statements) == 1
