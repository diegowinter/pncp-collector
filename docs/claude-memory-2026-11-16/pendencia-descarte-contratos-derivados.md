---
name: pendencia-descarte-contratos-derivados
description: Decisão em aberto (2026-09-08) sobre descartar ou apenas marcar contratos derivados de ata; depende de quem solicitou o projeto.
metadata: 
  node_type: memory
  type: project
  originSessionId: d173b0e1-8bdc-47de-a014-f75e376892da
  modified: 2026-09-09T02:42:45.868Z
---

Contrato derivado de ata já coletada hoje é **mantido e marcado** (`derived_from_ata`,
`derivation_match`), não descartado. A regra original que o Diego passou dizia
"descartar duplicidade ata ↔ contrato derivado"; ele preferiu manter os dois e marcar,
mas precisa confirmar com **quem solicitou o projeto** antes de fechar.

**Why:** manter e marcar é reversível (dá para filtrar na consulta e medir o tamanho da
sobreposição antes de decidir); descartar é irreversível sem recoletar.

**How to apply:** a escolha é a flag `PNCP_DISCARD_DERIVED_CONTRACTS` (padrão `false`) —
não mexer em código quando a resposta vier. Se virar descarte, os derivados já gravados
precisam de `DELETE FROM contratos WHERE derived_from_ata` ou `reset-db`.
Medição real de 2026-09-01: 4125 de 8074 contratos do dia traziam
`numeroControlePncpAta` preenchido. Ver [[escala-varredura-atas-pncp]].
