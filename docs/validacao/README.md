# Validação da triagem por objeto (fase 3)

Amostra de 2026-11-13: 200 compras com ata vigente (100 aleatórias, 60 com palavra-chave de
segurança, 40 com objeto genérico) e 40 pares ata/contrato da mesma compra com objetos
diferentes. Rotulagem humana: Diego rotulou 16 compras e os 40 pares e revisou as 184 restantes
preenchidas por Claude no mesmo critério (coluna `preenchido_por`).

| Arquivo | O que é |
|---|---|
| `amostra_triagem_2026-11-13.xlsx` | planilha rotulada (abas `instrucoes`, `compras`, `pares`) |
| `amostra_triagem_2026-11-13_jev.csv`, `amostra_pares_2026-11-13_jev.csv` | previsões da **primeira** versão do prompt, guardadas à parte para não enviesar a rotulagem |
| `resultado_validacao_2026-11-13.csv` | humano x modelo com o prompt final (`triage_rules.build_questions`), linha a linha |

## Resultado com o prompt e a regra finais (`typesafe/jev-1.14`)

Regra: `potencial` se a probabilidade global ≥ 0,45 **ou** algum grupo ≥ 0,5; `indefinido` se
genérico/remete ao edital; senão `fora_por_ora`.

| Humano | Modelo | só global, 0,5 | só global, 0,45 | **global 0,45 ou grupo 0,5** |
|---|---|---|---|---|
| sim (9) | potencial | 6 | 8 | **9** |
| talvez (17) | potencial ou indefinido | 16 | 17 | 17 |
| não (174) | potencial (falso positivo) | 2 | 3 | 3 |
| total marcado potencial | | 23/200 | 27/200 | 28/200 |

Por que o "ou" com os grupos: a pergunta global vê a média do objeto e se dilui quando ele
lista várias coisas ("mobiliário escolar, equipamentos de informática e playground" deu 0,15 de
potencial e 0,59 em `ti_comunicacao`; "gêneros alimentícios ... e uma motocicleta" deu 0,20 e
0,97 em `viaturas`). A pergunta do grupo enxerga o item escondido.

Ajustes de critério feitos a partir dos falsos positivos: `vestuario_operacional` passou a
exigir que o usuário seja força de segurança (uniforme de merendeira e de gari disparavam 0,5 a
0,6; caíram para menos de 0,1 sem perder fardamento da GCM, 0,98), e `viaturas` exclui serviço
feito com veículos.

Falsos positivos que restam: "materiais de campo para ACE (equipamentos/EPI)", "coletes de
segurança e identificação" e "serviços administrativos para pagamento de taxas" (0,50, sem
motivo aparente). Todos vão para a fase 2 e caem no classificador de itens.

Pares: 40/40 com a mesma decisão para o objeto da ata e o do contrato (humano e modelo). A
classificação da compra pode ser herdada pelos contratos derivados.

Decisão: `PNCP_TRIAGE_POTENTIAL_THRESHOLD = 0.45`, `PNCP_TRIAGE_GROUP_THRESHOLD = 0.5`. Custo
total das rodadas de validação: ~US$ 0,05.

# Validação da rotulagem de itens (fase 4)

Amostra de 2026-11-14: 300 descrições distintas de item sorteadas de `item_labels` (150 da
fatia `peneira`, 100 `aleatorio`, 50 `reserva`; ordem estável por `md5('validacao-2026-11-14' ||
descricao_norm)`). Pré-preenchida por Claude (Fable 5.1) com o critério de
`item_rules.CATEGORIAS`; Diego revisa na coluna `categoria_diego`.

| Arquivo | O que é |
|---|---|
| `amostra_itens_2026-11-14.xlsx` | planilha para revisão (abas `instrucoes`, `itens`) |
| `amostra_itens_2026-11-14_claude.csv` | as mesmas 300 com `descricao_norm` e o rótulo de Claude, para o treino **excluir** essas descrições e para medir o Jev e o classificador local contra elas |

Distribuição do pré-preenchimento: 249 `outros`, 29 `equip_ti`, 10 `viatura`, 4
`equip_comunicacao`, 2 `drone_rpas`, 2 `vestuario_operacional`, 2 `servico_seguranca`, 1
`protecao_balistica`, 1 `manutencao_controle_acesso`. Casos limite anotados em `obs_claude`
(smartphone genérico, triciclo de carga motorizado, catraca com instalação, descrição vazia
"LOTE Nº 46").

Calibração do Jev (200 descrições, 2026-11-14): US$ 0,000117 por descrição (2.842 tokens de
entrada), 36 de 120 da peneira fora de `outros`, 0 de 50 aleatórias, 1 de 30 da reserva. As
previsões do Jev para estas 300 entram em `amostra_itens_2026-11-14_jev.csv` depois do lote
completo.
