"""Regras puras da fase 2: arquivo alvo, homologacao, vencedor e maquina de estados."""

import pytest

from pncp_collector.config import Settings
from pncp_collector.detail_rules import (
    DEFAULT_REVISIT,
    decide_document_status,
    decide_items_status,
    has_target_file,
    homologated_items,
    is_target_file_type,
    items_needing_results,
    mark_winner,
    pick_winner,
    revisit_statuses,
    target_file_types,
)
from pncp_collector.models import DetailStatus, ItemsStatus, ResultsStatus

CONFIG = Settings(database_url="postgresql+psycopg://x/y")


# --- 6.1 arquivo alvo ------------------------------------------------------------


def test_defaults_de_tipo_alvo():
    assert target_file_types(CONFIG, "ata") == frozenset()
    assert target_file_types(CONFIG, "contrato") == {"Contrato", "Nota de Empenho"}


def test_override_por_config():
    config = CONFIG.model_copy(update={"target_file_types_ata": " Ata de Registro de Preço , Outros Documentos"})
    assert target_file_types(config, "ata") == {"Ata de Registro de Preço", "Outros Documentos"}


def test_conjunto_vazio_aceita_qualquer_tipo_mas_nao_lista_vazia():
    assert is_target_file_type("Outros Documentos", frozenset())
    assert is_target_file_type(None, frozenset())
    assert has_target_file([{"tipo_documento_nome": "Outros Documentos"}], frozenset())
    assert not has_target_file([], frozenset())


def test_comparacao_exata_com_strip():
    targets = frozenset({"Contrato"})
    assert is_target_file_type(" Contrato ", targets)
    assert not is_target_file_type("contrato", targets)
    assert not is_target_file_type("Contrato (termo inicial)", targets)
    assert not is_target_file_type(None, targets)


@pytest.mark.parametrize(
    "arquivos, expected",
    [
        ([{"tipo_documento_nome": "Contrato"}, {"tipo_documento_nome": "Outros Documentos"}], True),
        ([{"tipo_documento_nome": "Nota de Empenho"}], True),
        ([{"tipo_documento_nome": "Outros Documentos"}], False),
        ([{"tipo_documento_nome": "Edital"}], False),
        ([], False),
    ],
)
def test_has_target_file_contrato(arquivos, expected):
    assert has_target_file(arquivos, target_file_types(CONFIG, "contrato")) is expected


# --- 6.2 homologacao ---------------------------------------------------------------


def test_homologados_e_quem_precisa_de_resultado():
    itens = [
        {"numero_item": 1, "situacao_compra_item_nome": "Homologado", "tem_resultado": True},
        {"numero_item": 2, "situacao_compra_item_nome": "Homologado", "tem_resultado": False},
        {"numero_item": 3, "situacao_compra_item_nome": "Fracassado", "tem_resultado": False},
        {"numero_item": 4, "situacao_compra_item_nome": "Cancelado", "tem_resultado": True},
    ]
    assert [i["numero_item"] for i in homologated_items(itens)] == [1, 2]
    assert [i["numero_item"] for i in items_needing_results(itens)] == [1]
    assert decide_items_status(itens) == ItemsStatus.OK
    assert decide_items_status([]) == ItemsStatus.SEM_ITENS


# --- 6.3 vencedor ----------------------------------------------------------------


def r(seq, ordem, valor):
    return {"sequencial_resultado": seq, "ordem_classificacao_srp": ordem, "valor_unitario_homologado": valor}


def test_vencedor_um_resultado():
    assert pick_winner([r(1, 1, "0.0545")])["sequencial_resultado"] == 1


def test_vencedor_menor_ordem():
    res = [r(1, 3, 10), r(2, 1, 12), r(3, 2, 11)]
    assert pick_winner(res)["sequencial_resultado"] == 2


def test_ordem_none_com_valor_valido_ganha_de_ordem_1_com_valor_zero():
    res = [r(1, None, 5), r(2, 1, 0)]
    assert pick_winner(res)["sequencial_resultado"] == 1


def test_todos_com_valor_zero_ou_nulo_nao_tem_vencedor():
    assert pick_winner([r(1, 1, 0), r(2, 2, None), r(3, 3, "")]) is None
    assert pick_winner([]) is None


def test_empate_de_ordem_decide_pelo_sequencial():
    res = [r(5, 1, 9), r(2, 1, 9)]
    assert pick_winner(res)["sequencial_resultado"] == 2


def test_mark_winner_marca_so_um():
    res = mark_winner([r(1, 2, 10), r(2, 1, 10), r(3, None, 10)])
    assert [x["is_winner"] for x in res] == [False, True, False]
    assert all("is_winner" in x for x in mark_winner([r(1, 1, 0)]))
    assert mark_winner([r(1, 1, 0)])[0]["is_winner"] is False


# --- 6.4 maquina de estados --------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs, expected",
    [
        (dict(identified=False, has_file=True), (DetailStatus.SEM_IDENTIFICACAO, None)),
        # sem_identificacao vem antes de tudo, inclusive de erro
        (dict(identified=False, has_file=None, error="x"), (DetailStatus.SEM_IDENTIFICACAO, None)),
        (dict(identified=True, has_file=False), (DetailStatus.SEM_ARQUIVO, None)),
        # sem_arquivo antes de sem_homologado
        (
            dict(identified=True, has_file=False, items_status=ItemsStatus.SEM_ITENS),
            (DetailStatus.SEM_ARQUIVO, None),
        ),
        (dict(identified=True, has_file=None, error="HTTP 500"), (DetailStatus.ERRO, "HTTP 500")),
        (dict(identified=True, has_file=None), (DetailStatus.PENDENTE, None)),
        (dict(identified=True, has_file=True), (DetailStatus.PENDENTE, None)),
        (
            dict(identified=True, has_file=True, items_status=ItemsStatus.PENDENTE),
            (DetailStatus.PENDENTE, None),
        ),
        (
            dict(identified=True, has_file=True, items_status=ItemsStatus.SEM_ITENS),
            (DetailStatus.SEM_HOMOLOGADO, None),
        ),
        (
            dict(identified=True, has_file=True, items_status=ItemsStatus.OK, total_homologados=0),
            (DetailStatus.SEM_HOMOLOGADO, None),
        ),
        (
            dict(identified=True, has_file=True, items_status=ItemsStatus.ERRO, items_error="HTTP 502"),
            (DetailStatus.ERRO, "HTTP 502"),
        ),
        (
            dict(
                identified=True,
                has_file=True,
                items_status=ItemsStatus.OK,
                total_homologados=3,
                results_status=ResultsStatus.OK,
            ),
            (DetailStatus.OK, None),
        ),
        (
            dict(
                identified=True,
                has_file=True,
                items_status=ItemsStatus.OK,
                total_homologados=3,
                results_status=ResultsStatus.PARCIAL,
            ),
            (DetailStatus.PENDENTE, None),
        ),
        (
            dict(
                identified=True,
                has_file=True,
                items_status=ItemsStatus.OK,
                total_homologados=3,
                results_status=ResultsStatus.PENDENTE,
            ),
            (DetailStatus.PENDENTE, None),
        ),
        (
            dict(
                identified=True,
                has_file=True,
                items_status=ItemsStatus.OK,
                total_homologados=3,
                results_status=ResultsStatus.ERRO,
            ),
            (DetailStatus.ERRO, "resultados: erro"),
        ),
    ],
)
def test_decide_document_status(kwargs, expected):
    assert decide_document_status(**kwargs) == expected


def test_politica_de_revisita():
    assert revisit_statuses() == DEFAULT_REVISIT
    assert DetailStatus.SEM_ARQUIVO not in revisit_statuses()
    assert DetailStatus.OK not in revisit_statuses()
    assert DetailStatus.SEM_IDENTIFICACAO not in revisit_statuses(refresh=True, revisit_sem_arquivo=True)
    assert DetailStatus.SEM_ARQUIVO in revisit_statuses(revisit_sem_arquivo=True)
    assert DetailStatus.OK in revisit_statuses(refresh=True)
