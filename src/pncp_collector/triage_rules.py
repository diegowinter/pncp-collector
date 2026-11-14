"""Fase 3: regras da triagem por objeto. Puro: nada de rede nem banco.

A triagem le o objeto da compra (texto da capa) e decide se vale pagar a fase 2
por ela. Nada e descartado: cada compra recebe um `triage_status` e pode ser
reclassificada depois, porque as probabilidades e a resposta crua ficam na
linha.

Perguntas feitas ao modelo (endpoint de decisoes do OpenRouter):

    clareza    choice  claro | generico | remete_ao_edital
    potencial  noul    pode haver item de interesse para a pesquisa de precos?
    <grupo>    noul    um por grupo de categorias (ver GRUPOS)

Os grupos agrupam as 14 categorias de item do pesquisa-de-precos-pncp. Na capa
nao da para ir mais fundo que isso: "equipamentos de informatica" pode ser
notebook ou toner, e so o item diz. Por isso o criterio da capa e recall:
em duvida, `potencial`.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .models import TriageStatus

__all__ = ["TriageStatus", "Clarity", "GRUPOS", "build_questions", "build_state", "decide"]


class Clarity:
    CLARO = "claro"
    GENERICO = "generico"
    REMETE_AO_EDITAL = "remete_ao_edital"


# Grupo -> (descricao do que ENTRA, do que NAO ENTRA, categorias de item cobertas).
# As regras de entra/nao entra sao o resumo das do classificador de itens
# (pesquisa-de-precos-pncp, core/classification/categories.py), para que a capa
# e o item usem o mesmo criterio.
GRUPOS: dict[str, dict[str, Any]] = {
    "armamento": {
        "entra": (
            "Arma de fogo completa (pistola, revolver, fuzil, espingarda, carabina); arma nao "
            "letal (taser/arma de choque, spray OC/CS, bastao/tonfa, granada de efeito moral); "
            "municao de qualquer calibre, bala de borracha, cartucho 40mm; colete ou escudo "
            "balistico (nivel NIJ/NBR, 'balistico', 'a prova de bala'); capacete operacional "
            "(policial, tatico, antitumulto, balistico)."
        ),
        "nao_entra": (
            "Carregador, coldre, mira, luneta, estojo, kit de limpeza, alvo, replica ou airsoft, "
            "algema, colete tatico sem mencao balistica, capacete de moto, de obra ou de bombeiro."
        ),
        "categorias": ["arma_fogo", "arma_nao_letal", "municao", "protecao_balistica"],
    },
    "vestuario_operacional": {
        # "Uniforme" sozinho dispara em merendeira e gari; o que distingue e o
        # usuario ser forca de seguranca (validado em docs/validacao).
        "entra": (
            "Fardamento ou uniforme TATICO/OPERACIONAL de forca de seguranca (policia, guarda "
            "municipal, defesa civil, bombeiros, agentes de transito, forcas armadas), coturno, "
            "capa de colete, cinto de guarnicao."
        ),
        "nao_entra": (
            "Uniforme de merendeira, cozinheira, gari/coleta de lixo, motorista escolar, agente "
            "de saude, servidor administrativo ou aluno; uniforme social ou escolar; insignia ou "
            "bordado avulso; EPI de obra, limpeza ou saude; jaleco; camiseta promocional."
        ),
        "categorias": ["vestuario_operacional"],
    },
    "viaturas": {
        "entra": (
            "Veiculo motorizado completo: sedan, SUV, picape, hatch, motocicleta, van, "
            "micro-onibus, veiculo especial (comando movel, cela, resgate, unidade movel, "
            "ambulancia)."
        ),
        "nao_entra": (
            "Pneu, peca, oleo, lubrificante, pelicula, giroflex ou sirene avulsa, kit de "
            "caracterizacao, locacao de veiculo, combustivel, manutencao de frota, seguro; "
            "servico de transporte ou coleta FEITO com veiculos (coleta de lixo, transporte "
            "escolar, servico veterinario com veiculo)."
        ),
        "categorias": ["viatura"],
    },
    "ti_comunicacao": {
        "entra": (
            "Equipamento de TI completo: desktop, notebook, tablet, impressora, scanner, "
            "servidor; aparelho de comunicacao completo: radio HT portatil, radio veicular, "
            "radio base."
        ),
        "nao_entra": (
            "Toner, cartucho, monitor avulso, teclado, mouse, projetor, camera ou CFTV, telefone "
            "fixo, PABX, repetidora, bateria ou antena de radio, software, licenca, link de "
            "dados, telefonia movel, servico de TI."
        ),
        "categorias": ["equip_ti", "equip_comunicacao"],
    },
    "drone_bicicleta": {
        "entra": (
            "Drone/RPAS completo (multirrotor ou asa fixa, com camera) ou estacao de controle; "
            "bicicleta inteira para uso operacional (ciclopatrulha), inclusive eletrica."
        ),
        "nao_entra": (
            "Bateria ou helice de drone, capacete de ciclista, bicicleta ergometrica, "
            "patinete."
        ),
        "categorias": ["drone_rpas", "bicicleta"],
    },
    "servicos": {
        "entra": (
            "Organizacao de concurso publico, teste de aptidao fisica (TAF) ou avaliacao "
            "psicologica de processo seletivo; vigilancia ou monitoramento eletronico (CFTV, "
            "central de alarme, controle de acesso); aplicacao avulsa de testes de proficiencia "
            "ou aptidao; instalacao, manutencao ou locacao de controle de acesso fisico "
            "(catraca, cancela, biometria, fechadura eletronica)."
        ),
        "nao_entra": (
            "Vigilancia humana presencial, escolta, transporte de valores, brigada de incendio, "
            "portaria ou recepcao; curso de capacitacao; exame medico ocupacional; servico de "
            "instalacao de cameras; limpeza; manutencao predial; TI em geral."
        ),
        "categorias": [
            "servico_selecao",
            "servico_seguranca",
            "testes_proficiencia",
            "manutencao_controle_acesso",
        ],
    },
}

# Grupos que sozinhos (sem a pergunta global) levam a compra a `potencial`. So os
# de PRODUTO: o grupo `servicos` dispara em qualquer "prestacao de servicos"
# (plantao medico, servico postal, ar-condicionado...) e, sozinho, so trazia
# lixo numa amostra de 1.596 compras (13 casos, todos falsos). Servico de
# interesse de verdade (concurso, CFTV) tambem pontua na pergunta global.
STANDALONE_GROUPS = frozenset(g for g in GRUPOS if g != "servicos")

_DOMINIO = (
    "pesquisa de precos de itens de seguranca publica: armamento e municao, protecao "
    "balistica, fardamento tatico, viaturas, radios e equipamentos de TI, drones, "
    "bicicletas de patrulha, concursos/TAF, monitoramento eletronico e controle de acesso"
)


def build_questions() -> dict[str, dict[str, Any]]:
    """As perguntas tipadas do endpoint de decisoes. Deterministico (vai para o log)."""
    questions: dict[str, dict[str, Any]] = {
        "clareza": {
            "type": "choice",
            "instructions": (
                "O texto do objeto permite saber que tipo de bens ou servicos a contratacao "
                "fornece?"
            ),
            "criteria": {
                Clarity.CLARO: (
                    "Nomeia os bens ou servicos (ex.: 'aquisicao de pneus', 'medicamentos', "
                    "'notebooks e impressoras')."
                ),
                Clarity.GENERICO: (
                    "Generico demais para saber o que e (ex.: 'bens comuns', 'material de "
                    "consumo', 'materiais diversos', 'material permanente')."
                ),
                Clarity.REMETE_AO_EDITAL: (
                    "So remete ao edital, anexo, termo de referencia ou PDF, sem nomear os itens."
                ),
            },
        },
        "potencial": {
            "type": "noul",
            "instructions": (
                f"Esta contratacao pode conter itens de interesse para {_DOMINIO}? "
                "Responda pela POSSIBILIDADE: objeto generico ou amplo que possa incluir esses "
                "itens conta como possivel. So e 'nao' quando o objeto e claramente de outra "
                "area."
            ),
            "criteria": {
                "true": "Provavel ou possivel que haja itens dessas familias.",
                "false": (
                    "Claramente fora: medicamentos, material hospitalar, merenda e alimentos, "
                    "obras, limpeza, material escolar, eventos, combustivel, etc."
                ),
            },
        },
    }
    for group, meta in GRUPOS.items():
        questions[group] = {
            "type": "noul",
            "instructions": (
                f"O objeto indica fornecimento de itens do grupo '{group}'? Considere o "
                "produto-fim completo; pecas, acessorios e suprimentos nao contam."
            ),
            "criteria": {
                "true": f"ENTRA: {meta['entra']}",
                "false": f"NAO ENTRA (ou nao ha indicio): {meta['nao_entra']}",
            },
        }
    return questions


def build_state(objeto: str) -> dict[str, str]:
    return {"objeto_da_contratacao": objeto.strip()}


@dataclass
class TriageDecision:
    """O que vai para a linha da compra."""

    status: str
    clarity: str | None = None
    clarity_confidence: float | None = None
    potential: float | None = None
    groups: dict[str, float] = field(default_factory=dict)
    error: str | None = None


def decide(
    answers: dict[str, Any],
    *,
    potential_threshold: float = 0.45,
    group_threshold: float = 0.5,
) -> TriageDecision:
    """Da resposta do modelo ao status.

    1. potencial >= limiar, OU algum grupo >= group_threshold -> `potencial`,
       mesmo com objeto generico: a fase 2 vai dizer o que ha dentro. O "ou"
       existe porque a pergunta global se dilui em objetos mistos ("moveis,
       informatica e playground" deu 0,15 de potencial e 0,59 em ti_comunicacao;
       uma moto no meio de merenda deu 0,20 e 0,97 em viaturas). A pergunta do
       grupo enxerga o item escondido; a global ve a media.
    2. objeto generico ou "ver edital" -> `indefinido`: nao da para decidir pela
       capa; fica na fila do OCR dos arquivos (to-do).
    3. senao -> `fora_por_ora`.
    """
    try:
        clarity = answers["clareza"]
        potential = float(answers["potencial"]["noul"])
        groups = {g: round(float(answers[g]["noul"]), 4) for g in GRUPOS if g in answers}
    except (KeyError, TypeError, ValueError) as exc:
        return TriageDecision(status=TriageStatus.ERRO, error=f"resposta inesperada: {exc!r}")

    decision = TriageDecision(
        status=TriageStatus.FORA_POR_ORA,
        clarity=clarity.get("choice"),
        clarity_confidence=_as_float(clarity.get("confidence")),
        potential=round(potential, 4),
        groups=groups,
    )
    standalone = any(p >= group_threshold for g, p in groups.items() if g in STANDALONE_GROUPS)
    if potential >= potential_threshold or standalone:
        decision.status = TriageStatus.POTENCIAL
    elif decision.clarity in (Clarity.GENERICO, Clarity.REMETE_AO_EDITAL):
        decision.status = TriageStatus.INDEFINIDO
    return decision


def suggested_groups(groups: dict[str, float], threshold: float = 0.5) -> list[str]:
    return [g for g, p in sorted(groups.items(), key=lambda kv: -kv[1]) if p >= threshold]


def _as_float(value: Any) -> float | None:
    try:
        return None if value is None else round(float(value), 4)
    except (TypeError, ValueError):
        return None


def as_decimal(value: float | None) -> Decimal | None:
    return None if value is None else Decimal(str(value))


@dataclass
class TriageStats:
    compras_before: Counter = field(default_factory=Counter)
    compras_after: Counter = field(default_factory=Counter)
    seeded: int = 0
    processed: int = 0
    decided: Counter = field(default_factory=Counter)
    calls: int = 0
    errors: Counter = field(default_factory=Counter)
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "compras_before": dict(self.compras_before),
            "compras_after": dict(self.compras_after),
            "seeded": self.seeded,
            "processed": self.processed,
            "decided": dict(self.decided),
            "calls": self.calls,
            "errors": dict(self.errors),
            "cost_usd": round(self.cost_usd, 6),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "seconds": round(self.seconds, 1),
        }
