---
name: escala-varredura-atas-pncp
description: A varredura nacional de atas do PNCP não fecha em tempo viável do jeito atual; estratégia a definir (medido em 2026-09-08).
metadata: 
  node_type: memory
  type: project
  originSessionId: d173b0e1-8bdc-47de-a014-f75e376892da
  modified: 2026-09-09T02:42:54.758Z
---

Um único dia de atas não terminou em 9,5 minutos de coleta. O endpoint
`/api/consulta/v1/atas` filtra por vigência, então devolve **toda ata vigente naquele
dia** — a paginação é enorme e os primeiros registros são os mais antigos (quase todos
descartados pelo corte de 1 ano de assinatura). Multiplicado pelos 455 dias da janela, a
varredura nacional de atas não fecha em tempo razoável.

**Why:** não é bug do coletor nem falta de tuning — é o formato do endpoint. O delay de
0,5s e o backoff existem porque a API responde 429 com frequência; acelerar piora.

**How to apply:** o recorte `PNCP_CNPJ` contorna quando a pesquisa é de órgãos
específicos (foi como validei a coleta). Para varredura nacional, discutir paralelizar
por dia ou encurtar a janela retroativa antes de rodar valendo. Contratos não têm esse
problema: um dia inteiro leva ~6 min (83 páginas, 8220 registros).

**Hipótese não verificada (maior retorno):** se as páginas de um dia vierem numa ordem
estável e correlacionada com a publicação, as atas assinadas no último ano ficam
concentradas num extremo e dá para parar de paginar cedo, sem perder nada. Tentei medir
em 2026-09-09 e o endpoint de atas ficou timeoutando (mesmo com recorte por CNPJ) —
refazer quando a API estiver respondendo. Vale mais que qualquer checkpoint; ver
[[coleta-incremental-pncp]].
