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

## Resultado com o prompt final (`typesafe/jev-1.14`)

| Humano | Modelo | limiar 0,5 | limiar 0,45 |
|---|---|---|---|
| sim (9) | potencial | 6 | **8** |
| talvez (17) | potencial ou indefinido | 16 | 17 |
| não (174) | potencial (falso positivo) | 2 | 3 |
| total marcado potencial | | 23/200 | 27/200 |

Pares: 40/40 com a mesma decisão para o objeto da ata e o do contrato (humano e modelo). A
classificação da compra pode ser herdada pelos contratos derivados.

Perdido mesmo com 0,45: "mobiliário escolar, equipamentos de informática e playground" (0,16).
Falsos positivos: "materiais de campo para ACE (equipamentos/EPI)" e "coletes de segurança e
identificação" (palavras colete/EPI). Ambos vão para a fase 2 e caem no classificador de itens.

Decisão: limiar padrão `PNCP_TRIAGE_POTENTIAL_THRESHOLD = 0.45`. Custo da rodada de validação:
US$ 0,02 em 280 chamadas.
