"""Normalizacao dos payloads de detalhe (recortes reais de docs/API_DETALHE.md)."""

from datetime import date
from decimal import Decimal

from pncp_collector.schemas import normalize_arquivo, normalize_item, normalize_resultado

COMPRA = "07954480000179-1-025970/2025"

ITEM = {
    "numeroItem": 1,
    "descricao": "DIETA, NORMOCALORICA, ENRIQUECIDA COM TAURINA",
    "materialOuServico": "M",
    "materialOuServicoNome": "Material",
    "valorUnitarioEstimado": 0.0951,
    "valorTotal": 441073.8,
    "quantidade": 4638000.0,
    "unidadeMedida": "UNIDADE 1.0 MILILITRO ",
    "orcamentoSigiloso": False,
    "itemCategoriaId": 3,
    "itemCategoriaNome": "Não se aplica",
    "criterioJulgamentoId": 1,
    "criterioJulgamentoNome": "Menor preço",
    "situacaoCompraItem": 2,
    "situacaoCompraItemNome": "Homologado",
    "tipoBeneficio": 4,
    "tipoBeneficioNome": "Sem benefício",
    "dataInclusao": "2026-02-26T09:40:40",
    "dataAtualizacao": "2026-04-24T09:25:35",
    "temResultado": True,
    "ncmNbsCodigo": None,
    "catalogoCodigoItem": None,
}

RESULTADO = {
    "indicadorSubcontratacao": False,
    "reservaRemanescente": {"codigo": 1, "nome": "Não se aplica"},
    "numeroItem": 1,
    "niFornecedor": "49324221000104",
    "tipoPessoa": "PJ",
    "nomeRazaoSocialFornecedor": "FRESENIUS KABI BRASIL LTDA.",
    "valorTotalHomologado": 252771.0,
    "codigoPais": "BRA",
    "porteFornecedorId": 3,
    "quantidadeHomologada": 4638000.0,
    "valorUnitarioHomologado": 0.0545,
    "percentualDesconto": 0.0,
    "ordemClassificacaoSrp": 1,
    "dataResultado": "2026-04-24",
    "situacaoCompraItemResultadoId": 1,
    "porteFornecedorNome": "Demais",
    "situacaoCompraItemResultadoNome": "Informado",
    "sequencialResultado": 1,
    "aplicacaoMargemPreferencia": False,
    "numeroControlePNCPCompra": COMPRA,
}

ARQUIVO_ATA = {
    "url": "https://pncp.gov.br/pncp-api/v1/orgaos/07954480000179/compras/2025/25970/atas/1/arquivos/1",
    "tipoDocumentoNome": "Outros Documentos",
    "dataPublicacaoPncp": "2026-04-30T13:50:39",
    "sequencialDocumento": 1,
    "titulo": "TERMO DE HOMOLOGACAO.pdf",
    "tipoDocumentoId": 16,
}

ARQUIVO_CONTRATO = {
    "uri": "https://pncp.gov.br/pncp-api/v1/orgaos/07954480000179/contratos/2026/31999/arquivos/1",
    "url": "https://pncp.gov.br/pncp-api/v1/orgaos/07954480000179/contratos/2026/31999/arquivos/1",
    "dataPublicacaoPncp": "2026-09-01T16:10:28",
    "cnpj": "07954480000179",
    "anoCompra": 2026,
    "sequencialCompra": 31999,
    "sequencialDocumento": 1,
    "titulo": "20260901.1454621.Integra.CONTRATO",
    "tipoDocumentoNome": "Contrato",
}


def test_normalize_item():
    row = normalize_item(ITEM, COMPRA)
    assert row["compra_key"] == COMPRA
    assert row["numero_item"] == 1
    assert row["situacao_compra_item"] == 2
    assert row["situacao_compra_item_nome"] == "Homologado"
    assert row["tem_resultado"] is True
    assert row["quantidade"] == Decimal("4638000.0")
    assert row["valor_unitario_estimado"] == Decimal("0.0951")
    assert row["data_inclusao"] == date(2026, 2, 26)
    assert row["criterio_julgamento_nome"] == "Menor preço"
    assert row["ncm_nbs_codigo"] is None
    assert row["raw"] is ITEM


def test_normalize_resultado_injeta_chaves():
    row = normalize_resultado(RESULTADO, COMPRA, 1, 0)
    assert (row["compra_key"], row["numero_item"], row["sequencial_resultado"]) == (COMPRA, 1, 1)
    assert row["ordem_classificacao_srp"] == 1
    assert row["valor_unitario_homologado"] == Decimal("0.0545")
    assert row["ni_fornecedor"] == "49324221000104"
    assert row["porte_fornecedor_id"] == 3
    assert row["data_resultado"] == date(2026, 4, 24)
    assert row["indicador_subcontratacao"] is False
    # numeroControlePNCPCompra do payload nao vira coluna: a chave e a injetada.
    assert "numero_controle_pncp_compra" not in row


def test_normalize_resultado_sem_sequencial_usa_posicao():
    raw = {k: v for k, v in RESULTADO.items() if k != "sequencialResultado"}
    assert normalize_resultado(raw, COMPRA, 7, 2)["sequencial_resultado"] == 3
    assert normalize_resultado(raw, COMPRA, 7, 2)["numero_item"] == 7


def test_normalize_arquivo_ata_qualquer_tipo_conta():
    row = normalize_arquivo(ARQUIVO_ATA, "ata", "07954480000179-1-025970/2025-000001", frozenset())
    assert row["documento_tipo"] == "ata"
    assert row["documento_id"] == "07954480000179-1-025970/2025-000001"
    assert row["sequencial_documento"] == 1
    assert row["tipo_documento_id"] == 16
    assert row["tipo_documento_nome"] == "Outros Documentos"
    assert row["data_publicacao_pncp"] == date(2026, 4, 30)
    assert row["is_target_type"] is True


def test_normalize_arquivo_contrato_respeita_tipos_alvo():
    targets = frozenset({"Contrato", "Nota de Empenho"})
    row = normalize_arquivo(ARQUIVO_CONTRATO, "contrato", "07954480000179-2-031999/2026", targets)
    assert row["is_target_type"] is True
    assert "tipo_documento_id" not in row  # contrato nao traz
    assert row["url"].endswith("/contratos/2026/31999/arquivos/1")
    outro = normalize_arquivo(
        {**ARQUIVO_CONTRATO, "tipoDocumentoNome": "Outros Documentos"}, "contrato", "K", targets
    )
    assert outro["is_target_type"] is False
