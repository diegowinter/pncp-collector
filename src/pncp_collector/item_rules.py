"""Fase 4: regras da rotulagem de itens. Puro: nada de rede nem banco.

A fase 3 olha o objeto da capa; esta olha a DESCRICAO DO ITEM, que e onde o
produto aparece de verdade. O rotulo e por descricao distinta (normalizada), e
o destino dele e o conjunto de treino do classificador local de itens, nao a
classificacao de producao: o modelo de decisoes e o rotulador, o modelo
treinado e quem vai classificar os milhoes de itens.

Perguntas feitas ao modelo (endpoint de decisoes do OpenRouter):

    categoria  choice  uma das 14 categorias de item do pesquisa-de-precos-pncp
                       ou `outros`
    interesse  noul    e um produto/servico completo de uma das 14 categorias?

As 14 categorias e seus criterios seguem o classificador de itens do
pesquisa-de-precos-pncp (core/classification/categories.py), condensados.
Grafia de servicos: `servico_*` (convencao deste repositorio).

A PENEIRA (SIEVE_PATTERN) e a regex que escolhe as descricoes candidatas para
a amostra de treino. E proposital que erre para mais: pistola de cola e calibre
de cateter entram, e e bom que entrem, porque viram negativos dificeis.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .models import LabelStatus

__all__ = [
    "CATEGORIAS",
    "OUTROS",
    "SIEVE_PATTERN",
    "ItemLabelDecision",
    "ItemLabelStats",
    "build_item_questions",
    "build_item_state",
    "decide_item",
    "normalize_descricao",
    "sieve_regex",
]

OUTROS = "outros"

# categoria -> criterio (o que ENTRA e o que NAO ENTRA), na ordem do classificador
# de itens. "outros" por ultimo: e o fallback.
CATEGORIAS: dict[str, str] = {
    "arma_fogo": (
        "A arma de fogo em si: pistola, revolver, fuzil, espingarda, carabina, "
        "submetralhadora, rifle, em qualquer calibre, inclusive a que dispara municao nao "
        "letal. NAO ENTRA: cano, ferrolho, gatilho, carregador avulso, mira, red dot, luneta, "
        "coldre, cinto tatico, porta-carregador, kit de limpeza, alvo, replica, airsoft, "
        "simulacro, pistola de cola, pistola de pintura ou de pulverizacao."
    ),
    "arma_nao_letal": (
        "Dispositivo completo e funcional: dispositivo eletrico incapacitante (taser), spray "
        "de pimenta (OC), spray lacrimogeneo (CS), bastao retratil, tonfa policial, granada "
        "de efeito moral (luz e som). NAO ENTRA: granada explosiva ou letal, faca, algema, "
        "escudo antitumulto nao balistico, recarga ou cartucho de spray, bateria ou cartucho "
        "de taser, pecas e acessorios, bastao de cola, bastao esportivo."
    ),
    "municao": (
        "Municao completa de arma de fogo em qualquer calibre (9mm, .38, .40, .380, 5.56mm, "
        "7.62mm, .308, .50, calibre 12...), municao de elastomero (bala de borracha) e "
        "cartucho 40mm de gas lacrimogeneo ou impacto controlado. NAO ENTRA: espoleta, "
        "estojo vazio, projetil avulso, polvora, chumbinho ou BB de arma de pressao, alvo, "
        "caixa vazia, cartucho de toner ou de tinta, 'calibre' de cateter, sonda ou fio."
    ),
    "protecao_balistica": (
        "Colete, escudo ou capacete de uso operacional em seguranca publica ou defesa. Para "
        "COLETE ou ESCUDO e obrigatoria mencao explicita a balistica (balistico, a prova de "
        "bala, antibala, blindado, nivel NIJ/NBR II, IIIA, III, IV); sem ela, colete tatico e "
        "vestuario_operacional e os demais sao outros. CAPACETE operacional (militar, "
        "policial, tatico, antitumulto, balistico) entra sempre. NAO ENTRA: capacete de obra, "
        "de moto, de bombeiro civil, esportivo ou de bicicleta; capa ou porta-colete "
        "(vestuario_operacional); placa ou painel balistico avulso (pecas)."
    ),
    "equip_ti": (
        "Equipamento de TI completo: computador desktop, workstation, notebook, laptop, "
        "tablet, impressora, scanner de documentos, servidor de rede, storage, NAS, camera "
        "fotografica digital profissional, filmadora digital. NAO ENTRA: camera de seguranca "
        "ou CFTV, monitor avulso, projetor, teclado, mouse, toner, cartucho de tinta, papel, "
        "cabo, fonte, HD ou SSD avulso, memoria, placa, peca, suprimento ou periferico, "
        "software, licenca, servico de impressao ou outsourcing."
    ),
    "equip_comunicacao": (
        "Aparelho de comunicacao completo: radio comunicador portatil (HT, walkie-talkie), "
        "radio veicular, radio base fixo (analogico ou digital), smartphone corporativo para "
        "comunicacao operacional. NAO ENTRA: bateria, carregador, fonte, antena avulsa, capa, "
        "fone, microfone, headset, PTT, cabo, suporte veicular, clip, acessorio ou peca; "
        "telefone fixo, central PABX, repetidor ou estacao repetidora."
    ),
    "viatura": (
        "Veiculo motorizado completo: sedan, SUV, picape, hatchback, motocicleta, van, "
        "micro-onibus, caminhao, veiculo especial (comando movel, cela, resgate, unidade "
        "movel, ambulancia). NAO ENTRA: bicicleta (categoria bicicleta), pneu, camara de ar, "
        "peca, oleo, lubrificante, pelicula, giroflex ou sirene avulsa, kit de caracterizacao, "
        "radio veicular, equipamento embarcado, locacao de veiculo, manutencao, seguro, "
        "combustivel, servico feito com veiculo."
    ),
    "bicicleta": (
        "Bicicleta inteira para uso operacional de seguranca publica (ciclopatrulha, "
        "patrulhamento, ronda, guarda municipal, policia), inclusive eletrica. NAO ENTRA: "
        "bicicleta recreativa, ergometrica ou de uso civil generico; capacete, pneu, camara, "
        "peca, suporte, bagageiro ou acessorio."
    ),
    "drone_rpas": (
        "Drone completo (multirrotor, asa fixa, com camera termica, 4K ou zoom, com "
        "alto-falante ou holofote), sistema completo de drone ou estacao de controle "
        "terrestre. NAO ENTRA: aeromodelo recreativo, helice, bateria, controle remoto "
        "avulso, gimbal, camera avulsa, software isolado, peca, servico de voo ou de "
        "pulverizacao com drone."
    ),
    "vestuario_operacional": (
        "Vestuario e equipamento individual textil de uso operacional de forca de seguranca "
        "(policia, guarda municipal, bombeiros, defesa civil, transito, forcas armadas): capa "
        "ou porta-colete, colete tatico SEM mencao balistica, farda ou fardamento operacional, "
        "gandola, calca tatica, coturno ou bota tatica, luva tatica, cinto de guarnicao. NAO "
        "ENTRA: colete balistico (protecao_balistica), capacete (protecao_balistica), uniforme "
        "social ou administrativo, uniforme escolar ou de merendeira, gari ou agente de saude, "
        "EPI de obra ou de limpeza (botina de seguranca, luva de raspa), vestuario civil, "
        "insignia, distintivo, brasao, bordado, tarjeta ou emblema avulso."
    ),
    "servico_selecao": (
        "Servico de organizacao e realizacao de concurso publico (logistica, provas, "
        "aplicacao, correcao, resultados), teste de aptidao fisica (TAF), avaliacao "
        "psicologica ou psicotecnica vinculada a processo seletivo. NAO ENTRA: curso de "
        "capacitacao, treinamento interno, consultoria de RH, pericia medica admissional, "
        "exame ocupacional."
    ),
    "servico_seguranca": (
        "Servico de vigilancia eletronica (CFTV, cameras de seguranca, sensores), controle de "
        "acesso eletronico (catraca, biometria, cartao de proximidade, cancela automatizada), "
        "monitoramento eletronico (central de alarme, cerca eletrica), locacao de CFTV com "
        "monitoramento. NAO ENTRA: vigilancia humana presencial, seguranca armada ou "
        "desarmada, escolta, transporte de valores, brigada de incendio, portaria, recepcao; "
        "instalacao ou manutencao avulsa de cameras e alarmes."
    ),
    "testes_proficiencia": (
        "Servico de aplicacao avulsa de testes e avaliacoes de proficiencia, aptidao ou "
        "capacidade (psicologica, psicotecnica, educacional, cognitiva, TAF), sem ser a "
        "organizacao de um concurso inteiro. NAO ENTRA: concurso completo (servico_selecao), "
        "curso, treinamento, exame medico ocupacional."
    ),
    "manutencao_controle_acesso": (
        "Servico de instalacao, manutencao ou locacao de sistemas e equipamentos de controle "
        "de acesso fisico de pessoas: catraca, cancela, torniquete, leitor biometrico, "
        "fechadura eletronica, portao ou porta automatizada. NAO ENTRA: CFTV ou vigilancia "
        "eletronica (servico_seguranca), controle de acesso logico de TI, fornecimento do "
        "equipamento sem servico."
    ),
    OUTROS: (
        "Qualquer item que nao se encaixe estritamente numa das categorias acima: acessorios, "
        "pecas, componentes, suprimentos, baterias, carregadores, cabos, antenas, coldres, "
        "cintos, miras, lanternas, ferramentas, mobiliario, material de escritorio, "
        "medicamentos e material hospitalar, alimentos, material de construcao, cameras de "
        "seguranca e CFTV, monitores, projetores, telefones, software, locacoes e servicos em "
        "geral. EM CASO DE DUVIDA, ESCOLHA outros."
    ),
}

# Regex da peneira, em sintaxe do Postgres (ARE): `\y` e fronteira de palavra.
# Usada em `descricao ~* :pattern`. Para Python, ver sieve_regex().
SIEVE_PATTERN = (
    # armamento, municao, protecao
    r"pistola|rev[oó]lver|fuzil|carabina|espingarda|submetralhadora|muni[cç][aã]o|"
    r"cartucho (cal|\.|[0-9])|calibre ?\.?[0-9]|\.38\y|\.40\y|\.380\y|"
    r"taser|arma de choque|spray (de )?pimenta|lacrimog|\ytonfa|bast[aã]o (retr[aá]til|policial|tonfa)|"
    r"granada|bal[ií]stic|\ycolete|escudo|capacete|"
    # vestuario operacional
    r"coturno|fardamento|\yfarda\y|gandola|cal[cç]a t[aá]tica|cinto de guarni|porta[- ]colete|"
    # viaturas: "veiculo" e "caminhao" sozinhos puxam pneu, peca e locacao (20 mil e 5 mil
    # descricoes); so com o complemento que indica o veiculo inteiro
    r"viatura|motocicleta|camionete|caminhonete|picape|pick-?up|\ysuv\y|sedan|\yvan\y|furg[aã]o|"
    r"\yve[ií]culo (tipo|automotor|utilit|zero|novo|0 ?km|de passeio|leve|pesado|sedan|hatch|suv|"
    r"pick|van\y|furg|caminhonete|camionete|picape|4x4|4x2)|"
    r"caminh[aã]o (novo|zero|0 ?km|tipo|ba[uú]|bascul|toco|truck|3/4)|micro-?[oô]nibus|ambul[aâ]ncia|"
    # comunicacao
    r"r[aá]dio (comunicador|port[aá]til|transceptor|m[oó]vel|base|hts?|digital|veicular)|transceptor|"
    r"walkie|\yht\y|smartphone|c[aâ]mera corporal|"
    # servicos de seguranca e controle de acesso
    r"videomonitoramento|cftv|monitoramento eletr|controle de acesso|catraca|cancela|biometr|"
    # drone e bicicleta
    r"\ydrone|\yrpas\y|\yvant\y|aeronave remot|multirrotor|bicicleta|"
    # TI: "impressora" e "computador" sozinhos puxam toner, cabo e mesa (16 mil e 9 mil);
    # "nas" e preposicao e "9 mm" e medida de parafuso: fora
    r"notebook|laptop|microcomputador|desktop|workstation|tablet|scanner|storage|"
    r"computador (desktop|port[aá]til|pessoal|completo|tipo|all in one|de mesa|avan[cç]ado|"
    r"b[aá]sico|intermedi)|"
    r"impressora (multifuncional|laser|jato|t[eé]rmica|3d|matricial|monocrom|colorida|tipo|"
    r"de etiqueta|port[aá]til)|servidor (de rede|tipo|rack|torre)|"
    # servicos de selecao
    r"concurso p[uú]blico|aptid[aã]o f[ií]sica|\ytaf\y|psicot[eé]cnic|avalia[cç][aã]o psicol"
)


def sieve_regex() -> re.Pattern[str]:
    """A mesma peneira para Python (fronteira de palavra `\\b`)."""
    return re.compile(SIEVE_PATTERN.replace(r"\y", r"\b"), re.IGNORECASE)


_SPACES = re.compile(r"\s+")


def normalize_descricao(descricao: str) -> str:
    """Chave de `item_labels`. Tem de bater com o SQL em db.py:
    lower(regexp_replace(btrim(descricao), '\\s+', ' ', 'g'))."""
    return _SPACES.sub(" ", descricao.strip()).lower()


_DOMINIO = (
    "pesquisa de precos de itens de seguranca publica: armamento, municao, protecao "
    "balistica, fardamento operacional, viaturas, radios e equipamentos de TI, drones, "
    "bicicletas de patrulha, concursos/TAF, monitoramento eletronico e controle de acesso"
)


def build_item_questions() -> dict[str, dict[str, Any]]:
    """As perguntas tipadas do endpoint de decisoes. Deterministico (vai para o log)."""
    return {
        "categoria": {
            "type": "choice",
            "instructions": (
                "Classifique o ITEM descrito (nao a compra) numa unica categoria. Considere o "
                "produto-fim completo: peca, acessorio, suprimento, servico ou locacao do "
                f"produto vao para '{OUTROS}'. O objeto da compra e so contexto; a descricao "
                "do item decide."
            ),
            "criteria": dict(CATEGORIAS),
        },
        "interesse": {
            "type": "noul",
            "instructions": (
                f"O item e um produto ou servico completo de interesse para {_DOMINIO}, isto "
                f"e, cabe numa das categorias que nao sao '{OUTROS}'?"
            ),
            "criteria": {
                "true": "Produto-fim ou servico completo de uma das 14 categorias.",
                "false": (
                    "Peca, acessorio, suprimento, consumivel, locacao, manutencao ou item de "
                    "outra area (saude, alimentos, construcao, expediente, limpeza...)."
                ),
            },
        },
    }


def build_item_state(
    descricao: str,
    *,
    objeto: str | None = None,
    unidade_medida: str | None = None,
    valor_unitario_estimado: Decimal | float | None = None,
    material_ou_servico_nome: str | None = None,
) -> dict[str, str]:
    """O estado enviado ao modelo. So o que ajuda a decidir; a descricao manda."""
    state = {"descricao_do_item": descricao.strip()}
    if material_ou_servico_nome:
        state["material_ou_servico"] = material_ou_servico_nome
    if unidade_medida:
        state["unidade"] = unidade_medida.strip()
    if valor_unitario_estimado is not None:
        state["valor_unitario_estimado_reais"] = f"{Decimal(str(valor_unitario_estimado)):.2f}"
    if objeto:
        state["objeto_da_compra"] = objeto.strip()[:600]
    return state


@dataclass
class ItemLabelDecision:
    """O que vai para a linha de `item_labels`."""

    status: str
    category: str | None = None
    confidence: float | None = None
    interest: float | None = None
    probabilities: dict[str, float] = field(default_factory=dict)
    error: str | None = None


def decide_item(answers: dict[str, Any]) -> ItemLabelDecision:
    """Da resposta do modelo ao rotulo. Nao ha limiar aqui: o rotulo e a escolha
    do modelo, e as probabilidades ficam na linha para o treino decidir o que
    fazer com a faixa de duvida."""
    try:
        categoria = answers["categoria"]
        choice = str(categoria["choice"])
        probabilities = {
            str(k): round(float(v), 4) for k, v in (categoria.get("probabilities") or {}).items()
        }
        interest = float(answers["interesse"]["noul"])
    except (KeyError, TypeError, ValueError) as exc:
        return ItemLabelDecision(status=LabelStatus.ERRO, error=f"resposta inesperada: {exc!r}")
    if choice not in CATEGORIAS:
        return ItemLabelDecision(
            status=LabelStatus.ERRO, error=f"categoria desconhecida: {choice!r}"
        )
    confidence = _as_float(categoria.get("confidence"))
    if confidence is None:
        confidence = probabilities.get(choice)
    return ItemLabelDecision(
        status=LabelStatus.OK,
        category=choice,
        confidence=confidence,
        interest=round(interest, 4),
        probabilities=probabilities,
    )


def _as_float(value: Any) -> float | None:
    try:
        return None if value is None else round(float(value), 4)
    except (TypeError, ValueError):
        return None


@dataclass
class ItemLabelStats:
    labels_before: Counter = field(default_factory=Counter)
    labels_after: Counter = field(default_factory=Counter)
    seeded: Counter = field(default_factory=Counter)
    processed: int = 0
    decided: Counter = field(default_factory=Counter)
    categories: Counter = field(default_factory=Counter)
    calls: int = 0
    errors: Counter = field(default_factory=Counter)
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "labels_before": dict(self.labels_before),
            "labels_after": dict(self.labels_after),
            "seeded": dict(self.seeded),
            "processed": self.processed,
            "decided": dict(self.decided),
            "categories": dict(self.categories),
            "calls": self.calls,
            "errors": dict(self.errors),
            "cost_usd": round(self.cost_usd, 6),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "seconds": round(self.seconds, 1),
        }
