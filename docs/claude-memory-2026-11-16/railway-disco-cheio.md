---
name: railway-disco-cheio
description: "Em 2026-11-14 o Postgres do Railway ficou sem espaco (4,4 GB) no meio da coleta de contratos; copia local em pncp_railway (localhost:5432) feita como seguranca"
metadata:
  node_type: memory
  type: project
  originSessionId: 3cb4cb0f-fb70-4b8f-bd6a-48a4c8e872ba
  modified: 2026-11-14T03:52:22.528Z
---

- 2026-11-14 ~03:30 UTC: Railway Postgres respondeu "No space left on device". Banco 4.045 MB + 320 MB WAL. contratos 2,7 GB (1,25M linhas, faltavam 143 dias da janela), atas 1,1 GB, compras 0,2 GB. Coleta de contratos parou no dia 2026-05-27 (pagina 8/25).
- Projecao: contratos completos +1,5 GB; fase 2 (itens/arquivos com raw) mais alguns GB. Volume precisa de ~10 GB. Resolver no painel Railway (Settings > Volume > Grow) ou liberar espaco (contratos derivados = 385k linhas, ~0,9 GB; ver [[pendencia-descarte-contratos-derivados]]).
- Servidor e PostgreSQL 18; pg_dump local e 16 (nao dumpa servidor mais novo). Copia feita com `scripts/copiar_banco.py` (COPY binario em streaming, tabela a tabela) para o Postgres local `postgresql://postgres:postgres@localhost:5432/pncp_railway`.
- Depois de crescer o volume: `uv run pncp-collector contratos` na caixa retoma da pagina onde parou.

**Why:** a coleta nacional de contratos e muito maior que o previsto (4,5k/dia; 1,8M no total) e o volume padrao nao comporta.
**How to apply:** antes de rodar fase 2 ou retomar contratos, confirmar espaco livre no Railway. Ver [[fase3-triagem-objeto-jev]].
