from datetime import date

from pncp_collector.config import Settings
from pncp_collector.filters import DiscardReason, SuspicionReason, check_record
from pncp_collector.pipeline import Collector, date_range
from pncp_collector.schemas import normalize_ata, normalize_contrato

CONFIG = Settings(database_url="postgresql+psycopg://x/y")
TODAY = date(2026, 9, 8)


def base_ata(**overrides):
    raw = {
        "numeroControlePNCPAta": "1-1-1/2026-000001",
        "numeroControlePNCPCompra": "1-1-1/2026",
        "cancelado": False,
        "dataCancelamento": None,
        "dataAssinatura": "2026-06-01",
        "vigenciaInicio": "2026-06-01",
        "vigenciaFim": "2027-06-01",
        "cnpjOrgao": "18457226000181",
    }
    raw.update(overrides)
    return normalize_ata(raw)


def test_mantem_assinatura_recente():
    discard, reasons = check_record(base_ata(), CONFIG, TODAY)
    assert discard is None
    assert reasons == []


def test_descarta_assinatura_antiga_mesmo_vigente():
    record = base_ata(dataAssinatura="2023-06-16", vigenciaFim="2030-10-07")
    discard, _ = check_record(record, CONFIG, TODAY)
    assert discard == DiscardReason.OLD_SIGNATURE


def test_descarta_sem_data_assinatura():
    discard, _ = check_record(base_ata(dataAssinatura=None), CONFIG, TODAY)
    assert discard == DiscardReason.NO_SIGNATURE


def test_descarta_cancelado():
    record = base_ata(cancelado=True, dataCancelamento="2026-07-01")
    assert check_record(record, CONFIG, TODAY)[0] == DiscardReason.CANCELLED


def test_limite_exato_de_365_dias():
    record = base_ata(dataAssinatura="2025-09-08")
    assert check_record(record, CONFIG, TODAY)[0] is None
    record = base_ata(dataAssinatura="2025-09-07")
    assert check_record(record, CONFIG, TODAY)[0] == DiscardReason.OLD_SIGNATURE


def test_suspeito_vigencia_longa_e_inicio_antes_da_assinatura():
    record = base_ata(vigenciaInicio="2026-05-01", vigenciaFim="2054-10-23")
    discard, reasons = check_record(record, CONFIG, TODAY)
    assert discard is None
    assert SuspicionReason.LONG_VALIDITY in reasons
    assert SuspicionReason.VALIDITY_BEFORE_SIGNATURE in reasons


def test_normaliza_contrato_com_estruturas_aninhadas():
    record = normalize_contrato(
        {
            "numeroControlePNCP": "9-2-3/2026",
            "numeroControlePncpCompra": "9-1-3/2026",
            "dataAssinatura": "2026-08-01",
            "dataVigenciaInicio": "2026-08-01",
            "dataVigenciaFim": "2027-08-01",
            "valorGlobal": "1234.56",
            "orgaoEntidade": {"cnpj": "00.394.452/0001-03", "razaoSocial": "ORGAO"},
            "unidadeOrgao": {"codigoUnidade": "160434", "ufSigla": "RS"},
            "tipoContrato": {"id": 1, "nome": "Contrato"},
        }
    )
    assert record["agency_cnpj"] == "00394452000103"
    assert str(record["global_value"]) == "1234.56"
    assert record["unit_uf"] == "RS"
    assert check_record(record, CONFIG, TODAY)[0] is None


def test_janelas_de_varredura():
    collector = Collector.__new__(Collector)
    collector.config = CONFIG
    atas_days = collector.ata_days(TODAY)
    assert atas_days[0] == date(2025, 9, 9)
    assert atas_days[-1] == TODAY.replace(day=7, month=12)
    assert len(atas_days) == 365 + 90
    contratos_days = collector.contrato_days(TODAY)
    assert contratos_days[0] == date(2025, 8, 4)
    assert len(contratos_days) == 401


def test_date_range_invertido_vazio():
    assert date_range(date(2026, 1, 2), date(2026, 1, 1)) == []
