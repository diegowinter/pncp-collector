"""Modelo de dados (SQLAlchemy 2.0) das atas e contratos coletados.

Convencao de nomes: as colunas que vem do PNCP mantem o nome da API, apenas
convertido de camelCase para snake_case (`dataAssinatura` -> `data_assinatura`),
inclusive as de objetos aninhados, que carregam o caminho no nome
(`orgaoEntidade.razaoSocial` -> `orgao_entidade_razao_social`). So os campos de
controle nossos ficam em ingles: `collected_at`, `updated_at`, `raw`,
`suspicious`, `derived_from_ata`, `detail_status`, `is_winner` e afins.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class StampMixin:
    """Quando a linha entrou e quando foi tocada pela ultima vez."""

    collected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ControlMixin(StampMixin):
    """Campos de controle nossos das capas, sem contrapartida no PNCP."""

    suspicious: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    suspicious_reasons: Mapped[list[str] | None] = mapped_column(JSONB)
    raw: Mapped[dict] = mapped_column(JSONB)


class DetailStatus:
    """Estado do detalhamento (fase 2) de uma capa. Ver README, § Detalhamento."""

    PENDENTE = "pendente"
    OK = "ok"
    SEM_IDENTIFICACAO = "sem_identificacao"
    SEM_ARQUIVO = "sem_arquivo"
    SEM_HOMOLOGADO = "sem_homologado"
    ERRO = "erro"


class ItemsStatus:
    """Estado da busca de itens de uma compra."""

    PENDENTE = "pendente"
    OK = "ok"
    SEM_ITENS = "sem_itens"
    ERRO = "erro"


class ResultsStatus:
    """Estado da busca de resultados de uma compra (N chamadas; pode parar no meio)."""

    PENDENTE = "pendente"
    OK = "ok"
    PARCIAL = "parcial"
    ERRO = "erro"


class DetailMixin:
    """Estado da fase 2 por documento. Persistido para retomar de onde parou."""

    detail_status: Mapped[str] = mapped_column(
        String(30),
        default=DetailStatus.PENDENTE,
        server_default=DetailStatus.PENDENTE,
        index=True,
    )
    detail_error: Mapped[str | None] = mapped_column(Text)
    detailed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    detail_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # FK logica para compras.compra_key; preenchida na resolucao (passo A).
    compra_key: Mapped[str | None] = mapped_column(String(80), index=True)


class Ata(Base, ControlMixin, DetailMixin):
    """Ata de registro de precos. GET /api/consulta/v1/atas."""

    __tablename__ = "atas"

    numero_controle_pncp_ata: Mapped[str] = mapped_column(String(80), primary_key=True)
    numero_controle_pncp_compra: Mapped[str | None] = mapped_column(
        String(80), index=True
    )
    numero_ata_registro_preco: Mapped[str | None] = mapped_column(String(120))
    ano_ata: Mapped[int | None] = mapped_column(Integer)

    data_assinatura: Mapped[date] = mapped_column(Date, index=True)
    vigencia_inicio: Mapped[date | None] = mapped_column(Date)
    vigencia_fim: Mapped[date | None] = mapped_column(Date)
    data_publicacao_pncp: Mapped[date | None] = mapped_column(Date, index=True)
    data_inclusao: Mapped[date | None] = mapped_column(Date)
    data_atualizacao: Mapped[date | None] = mapped_column(Date)
    data_atualizacao_global: Mapped[date | None] = mapped_column(Date)

    cancelado: Mapped[bool] = mapped_column(Boolean, default=False)
    data_cancelamento: Mapped[date | None] = mapped_column(Date)

    objeto_contratacao: Mapped[str | None] = mapped_column(Text)

    cnpj_orgao: Mapped[str | None] = mapped_column(String(20), index=True)
    nome_orgao: Mapped[str | None] = mapped_column(Text)
    cnpj_orgao_subrogado: Mapped[str | None] = mapped_column(String(20))
    nome_orgao_subrogado: Mapped[str | None] = mapped_column(Text)
    codigo_unidade_orgao: Mapped[str | None] = mapped_column(String(40))
    nome_unidade_orgao: Mapped[str | None] = mapped_column(Text)
    codigo_unidade_orgao_subrogado: Mapped[str | None] = mapped_column(String(40))
    nome_unidade_orgao_subrogado: Mapped[str | None] = mapped_column(Text)

    usuario: Mapped[str | None] = mapped_column(Text)
    possibilidade_adesao: Mapped[bool | None] = mapped_column(Boolean)


class Contrato(Base, ControlMixin, DetailMixin):
    """Contrato. GET /api/consulta/v1/contratos.

    A API nao devolve `cancelado`/`dataCancelamento` para contratos, entao essas
    colunas nao existem aqui.
    """

    __tablename__ = "contratos"

    numero_controle_pncp: Mapped[str] = mapped_column(String(80), primary_key=True)
    numero_controle_pncp_compra: Mapped[str | None] = mapped_column(
        String(80), index=True
    )
    numero_controle_pncp_ata: Mapped[str | None] = mapped_column(String(80), index=True)
    numero_contrato_empenho: Mapped[str | None] = mapped_column(String(120))
    ano_contrato: Mapped[int | None] = mapped_column(Integer)
    sequencial_contrato: Mapped[int | None] = mapped_column(Integer)
    processo: Mapped[str | None] = mapped_column(String(120))

    data_assinatura: Mapped[date] = mapped_column(Date, index=True)
    data_vigencia_inicio: Mapped[date | None] = mapped_column(Date)
    data_vigencia_fim: Mapped[date | None] = mapped_column(Date)
    data_publicacao_pncp: Mapped[date | None] = mapped_column(Date, index=True)
    data_atualizacao: Mapped[date | None] = mapped_column(Date)
    data_atualizacao_global: Mapped[date | None] = mapped_column(Date)

    objeto_contrato: Mapped[str | None] = mapped_column(Text)
    informacao_complementar: Mapped[str | None] = mapped_column(Text)

    ni_fornecedor: Mapped[str | None] = mapped_column(String(30), index=True)
    tipo_pessoa: Mapped[str | None] = mapped_column(String(2))
    nome_razao_social_fornecedor: Mapped[str | None] = mapped_column(Text)

    valor_inicial: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    valor_parcela: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    valor_global: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    valor_acumulado: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    numero_parcelas: Mapped[int | None] = mapped_column(Integer)
    receita: Mapped[bool | None] = mapped_column(Boolean)

    orgao_entidade_cnpj: Mapped[str | None] = mapped_column(String(20), index=True)
    orgao_entidade_razao_social: Mapped[str | None] = mapped_column(Text)
    orgao_entidade_poder_id: Mapped[str | None] = mapped_column(String(2))
    orgao_entidade_esfera_id: Mapped[str | None] = mapped_column(String(2))

    unidade_orgao_codigo_unidade: Mapped[str | None] = mapped_column(String(40))
    unidade_orgao_nome_unidade: Mapped[str | None] = mapped_column(Text)
    unidade_orgao_uf_sigla: Mapped[str | None] = mapped_column(String(2))
    unidade_orgao_municipio_nome: Mapped[str | None] = mapped_column(Text)
    unidade_orgao_codigo_ibge: Mapped[str | None] = mapped_column(String(10))

    tipo_contrato_id: Mapped[int | None] = mapped_column(Integer)
    tipo_contrato_nome: Mapped[str | None] = mapped_column(Text)
    categoria_processo_id: Mapped[int | None] = mapped_column(Integer)
    categoria_processo_nome: Mapped[str | None] = mapped_column(Text)

    usuario_nome: Mapped[str | None] = mapped_column(Text)

    # Controle nosso: o contrato deriva de uma ata que tambem esta na base.
    derived_from_ata: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    derivation_match: Mapped[str | None] = mapped_column(String(20))
    # De onde saiu compra_key: "capa" (numero_controle_pncp_compra) ou "fallback"
    # (chamada extra a /orgaos/{cnpj}/contratos/{ano}/{seq}).
    compra_resolved_by: Mapped[str | None] = mapped_column(String(20))


Index("ix_atas_assinatura_orgao", Ata.data_assinatura, Ata.cnpj_orgao)
Index(
    "ix_contratos_assinatura_orgao",
    Contrato.data_assinatura,
    Contrato.orgao_entidade_cnpj,
)


class Compra(Base, StampMixin):
    """Uma compra (pregao/contratacao): a unidade da fase 2.

    Itens e resultados sao consultados uma vez por compra e ligados as N atas e
    contratos que saem dela. Nao tem `raw`: nao existe um payload "da compra"
    nesta etapa, so o dos itens.
    """

    __tablename__ = "compras"

    compra_key: Mapped[str] = mapped_column(String(80), primary_key=True)
    cnpj: Mapped[str] = mapped_column(String(14), index=True)
    ano: Mapped[int] = mapped_column(Integer)
    sequencial: Mapped[int] = mapped_column(Integer)
    url_pncp: Mapped[str | None] = mapped_column(Text)

    items_status: Mapped[str] = mapped_column(
        String(30),
        default=ItemsStatus.PENDENTE,
        server_default=ItemsStatus.PENDENTE,
        index=True,
    )
    items_error: Mapped[str | None] = mapped_column(Text)
    items_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    items_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    total_itens: Mapped[int | None] = mapped_column(Integer)
    total_homologados: Mapped[int | None] = mapped_column(Integer)

    results_status: Mapped[str] = mapped_column(
        String(30),
        default=ResultsStatus.PENDENTE,
        server_default=ResultsStatus.PENDENTE,
        index=True,
    )
    results_error: Mapped[str | None] = mapped_column(Text)
    results_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    results_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class Item(Base, StampMixin):
    """Item de uma compra. GET /api/pncp/v1/orgaos/{cnpj}/compras/{ano}/{seq}/itens.

    Todos os itens sao gravados com a situacao que vieram; so `Homologado`
    conta para o status do documento (regra em detail_rules).
    """

    __tablename__ = "itens"

    compra_key: Mapped[str] = mapped_column(String(80), primary_key=True)
    numero_item: Mapped[int] = mapped_column(Integer, primary_key=True)

    descricao: Mapped[str | None] = mapped_column(Text)
    material_ou_servico: Mapped[str | None] = mapped_column(String(2))
    material_ou_servico_nome: Mapped[str | None] = mapped_column(String(40))
    unidade_medida: Mapped[str | None] = mapped_column(Text)
    quantidade: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    valor_unitario_estimado: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    valor_total: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    orcamento_sigiloso: Mapped[bool | None] = mapped_column(Boolean)

    # A API chama de situacaoCompraItem (sem sufixo Id); o nome segue a API.
    situacao_compra_item: Mapped[int | None] = mapped_column(Integer)
    situacao_compra_item_nome: Mapped[str | None] = mapped_column(String(60), index=True)
    tem_resultado: Mapped[bool | None] = mapped_column(Boolean)

    criterio_julgamento_id: Mapped[int | None] = mapped_column(Integer)
    criterio_julgamento_nome: Mapped[str | None] = mapped_column(Text)
    item_categoria_id: Mapped[int | None] = mapped_column(Integer)
    item_categoria_nome: Mapped[str | None] = mapped_column(Text)
    tipo_beneficio: Mapped[int | None] = mapped_column(Integer)
    tipo_beneficio_nome: Mapped[str | None] = mapped_column(Text)
    ncm_nbs_codigo: Mapped[str | None] = mapped_column(String(40))
    ncm_nbs_descricao: Mapped[str | None] = mapped_column(Text)
    catalogo_codigo_item: Mapped[str | None] = mapped_column(String(80))
    informacao_complementar: Mapped[str | None] = mapped_column(Text)

    data_inclusao: Mapped[date | None] = mapped_column(Date)
    data_atualizacao: Mapped[date | None] = mapped_column(Date)

    raw: Mapped[dict] = mapped_column(JSONB)


class ItemResultado(Base, StampMixin):
    """Resultado (proposta homologada) de um item.

    GET .../itens/{numeroItem}/resultados. Todos os resultados sao gravados;
    `is_winner` marca o escolhido pela regra de preco (detail_rules.pick_winner).
    """

    __tablename__ = "itens_resultados"

    compra_key: Mapped[str] = mapped_column(String(80), primary_key=True)
    numero_item: Mapped[int] = mapped_column(Integer, primary_key=True)
    sequencial_resultado: Mapped[int] = mapped_column(Integer, primary_key=True)

    ordem_classificacao_srp: Mapped[int | None] = mapped_column(Integer)
    ni_fornecedor: Mapped[str | None] = mapped_column(String(30), index=True)
    nome_razao_social_fornecedor: Mapped[str | None] = mapped_column(Text)
    tipo_pessoa: Mapped[str | None] = mapped_column(String(2))
    codigo_pais: Mapped[str | None] = mapped_column(String(3))
    porte_fornecedor_id: Mapped[int | None] = mapped_column(Integer)
    porte_fornecedor_nome: Mapped[str | None] = mapped_column(Text)

    quantidade_homologada: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    valor_unitario_homologado: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    valor_total_homologado: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    percentual_desconto: Mapped[Decimal | None] = mapped_column(Numeric(9, 4))

    situacao_compra_item_resultado_id: Mapped[int | None] = mapped_column(Integer)
    situacao_compra_item_resultado_nome: Mapped[str | None] = mapped_column(String(60))
    data_resultado: Mapped[date | None] = mapped_column(Date)
    data_cancelamento: Mapped[date | None] = mapped_column(Date)
    motivo_cancelamento: Mapped[str | None] = mapped_column(Text)

    indicador_subcontratacao: Mapped[bool | None] = mapped_column(Boolean)
    aplicacao_margem_preferencia: Mapped[bool | None] = mapped_column(Boolean)
    aplicacao_beneficio_me_epp: Mapped[bool | None] = mapped_column(Boolean)
    aplicacao_criterio_desempate: Mapped[bool | None] = mapped_column(Boolean)

    # Controle nosso: o vencedor segundo a regra de preco.
    is_winner: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    raw: Mapped[dict] = mapped_column(JSONB)


class Arquivo(Base, StampMixin):
    """Arquivo listado de uma ata ou de um contrato. Nada e baixado.

    Pertence ao documento, nao a compra: `documento_id` e
    `numero_controle_pncp_ata` ou `numero_controle_pncp`.
    """

    __tablename__ = "arquivos"

    documento_tipo: Mapped[str] = mapped_column(String(10), primary_key=True)
    documento_id: Mapped[str] = mapped_column(String(80), primary_key=True, index=True)
    sequencial_documento: Mapped[int] = mapped_column(Integer, primary_key=True)

    # tipoDocumentoId so vem para ata; contrato nao traz.
    tipo_documento_id: Mapped[int | None] = mapped_column(Integer)
    tipo_documento_nome: Mapped[str | None] = mapped_column(String(120), index=True)
    titulo: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    data_publicacao_pncp: Mapped[date | None] = mapped_column(Date)

    # Controle nosso: conta como "arquivo alvo" para o status sem_arquivo?
    is_target_type: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    raw: Mapped[dict] = mapped_column(JSONB)


Index("ix_itens_situacao_compra", Item.situacao_compra_item_nome, Item.compra_key)


class CollectionRun(Base):
    """Registro de cada execucao. Tabela nossa: tudo em ingles."""

    __tablename__ = "collection_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dataset: Mapped[str] = mapped_column(String(20))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    window_start: Mapped[date | None] = mapped_column(Date)
    window_end: Mapped[date | None] = mapped_column(Date)
    stats: Mapped[dict | None] = mapped_column(JSONB)


class CollectionDay(Base):
    """Dia da fase 1 ja gravado por inteiro: a proxima execucao com a mesma
    referencia pula esse dia. Dia com falha nao entra, e fica para a retomada."""

    __tablename__ = "collection_days"

    dataset: Mapped[str] = mapped_column(String(20), primary_key=True)
    # Data de referencia da varredura (o "hoje" que define a janela).
    reference: Mapped[date] = mapped_column(Date, primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    records: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    finished_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class CollectionDayProgress(Base):
    """Dia da fase 1 em andamento: quantas paginas ja foram gravadas. Some quando
    o dia vai para collection_days. Uma retomada continua da pagina seguinte."""

    __tablename__ = "collection_day_progress"

    dataset: Mapped[str] = mapped_column(String(20), primary_key=True)
    reference: Mapped[date] = mapped_column(Date, primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    pages_done: Mapped[int] = mapped_column(Integer)
    total_pages: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
