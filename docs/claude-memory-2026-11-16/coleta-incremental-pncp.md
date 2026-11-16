---
name: coleta-incremental-pncp
description: "Demanda aberta (2026-09-09) de evitar requisições desperdiçadas na varredura; contratos podem pular dias já coletados, atas não."
metadata: 
  node_type: memory
  type: project
  originSessionId: d173b0e1-8bdc-47de-a014-f75e376892da
  modified: 2026-09-09T14:32:05.393Z
---

Hoje a varredura ignora o que já está no banco: refaz todas as requisições, reprocessa e
regrava por upsert (verificado — reexecutar o mesmo dia manteve 391 linhas e só mudou o
`updated_at`). O Diego quer evitar a requisição desperdiçada. A API entrega por **dia**,
não por registro, então só dá para economizar pulando dias inteiros ou cortando
paginação — nunca no nível do registro.

Assimetria que decide o desenho:

- **Contratos** (filtro por publicação): dia passado é imutável, pode ser pulado com
  segurança. Ressalva: rebuscar o dia é o que traz atualizações (retificação,
  cancelamento); pular congela o que foi gravado. Precisa de cauda de re-varredura
  configurável.
- **Atas** (filtro por vigência): o conjunto de um dia passado **muda com o tempo** — ata
  assinada depois, com vigência retroativa, passa a aparecer em dias já concluídos
  (defasagem assinatura → publicação chega a 3 meses nos dados reais). Pular deixa
  registro de fora. Checkpoint em atas só serve para retomar execução interrompida.

**Why:** o Diego perguntou explicitamente se dá para pular "sem deixar nada de fora" — a
resposta é sim para contratos e não para atas, e confundir os dois causa perda silenciosa
de dados.

**How to apply:** desenho proposto, três frentes independentes — (1) tabela
`collection_days(dataset, day, status, ...)` escrita ao fim de cada dia, para pular em
contratos e retomar em atas; (2) cauda de re-varredura em contratos; (3) investigar a
ordenação das páginas de atas, ver [[escala-varredura-atas-pncp]]. A (3) é a de maior
retorno e ainda depende de medição.
