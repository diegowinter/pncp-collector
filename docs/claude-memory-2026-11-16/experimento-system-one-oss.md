---
name: experimento-system-one-oss
description: "Resultado do experimento de 2026-11-14 com modelos System One abertos (Laya, Von) para a triagem por objeto, e a comparacao noul x choice no Jev"
metadata:
  node_type: memory
  type: project
  originSessionId: 3cb4cb0f-fb70-4b8f-bd6a-48a4c8e872ba
  modified: 2026-11-14T03:16:03.962Z
---

Branch `experimento/system-one-oss` (commit a31870f), pasta `experimentos/system_one_oss/` com venv proprio (nao mexe no projeto). Gabarito: as 200 compras validadas.

- **Laya multilingual** (322M, CPU 1,2-1,6 s/compra): zero-shot com as 8 perguntas de producao e inutil (AUC 0,41, marca tudo potencial; noul sai ~1 sempre). Em `choice` curta AUC 0,80, mas 54 falsos positivos em 174 (Jev: 3). So fica competitivo com fine-tune; caminho sugerido: destilar rotulos do Jev no lote inteiro e ajustar o Laya.
- **Von** (ModernBERT ingles): nao le portugues, descartado.
- **Jev noul x choice** (pergunta do Diego): equivalentes. Choice detalhado tem AUC 0,996 e custa 20% menos, mas puxa perifericos para "outros bens" e so da 8/9 com limiar 0,30. Decisao: manter o desenho de producao (noul por grupo com "ou"); reavaliar apos o lote completo.
- Diego tem a intuicao de que `choice` e mais adequado; os dados dizem que no Jev tanto faz e no Laya choice e muito melhor.

**Why:** Diego quer processar "em casa" (sem GPU) se a qualidade chegar perto; o custo do Jev e baixo (US$25 o lote), entao a motivacao e independencia, nao economia.
**How to apply:** nao trocar a triagem de producao por modelo aberto sem fine-tune validado nas 200 compras humanas. Ver [[fase3-triagem-objeto-jev]].
