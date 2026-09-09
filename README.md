# pncp-collector

Coletor de **atas de registro de preços** e **contratos** do PNCP para pesquisa de preços,
com janela de 1 ano de assinatura. Persiste em Postgres, mostra progresso com `rich`.

## Instalação

```bash
uv sync
cp .env.example .env   # ajuste PNCP_DATABASE_URL
uv run pncp-collector init-db
```

## Uso

```bash
uv run pncp-collector atas          # varredura por vigência
uv run pncp-collector contratos     # varredura por publicação
uv run pncp-collector all           # atas e depois contratos (a ordem importa)
uv run pncp-collector all --reference 2026-09-08 -v
```

`all` roda as atas primeiro de propósito: o descarte de contrato derivado de ata
consulta as atas já persistidas.

## Regra de coleta

Uma chamada por dia, com `dataInicial == dataFinal`, paginando de 100 em 100.

| Dataset | Endpoint | Filtro da API | Janela |
| --- | --- | --- | --- |
| Atas | `/api/consulta/v1/atas` | vigência | `hoje-364 … hoje` e `hoje+1 … hoje+90` |
| Contratos | `/api/consulta/v1/contratos` | publicação | `hoje-400 … hoje` |

- **Atas**: dedup obrigatório por `numeroControlePNCPAta` — a mesma ata volta em todos
  os dias cobertos pela vigência.
- **Contratos**: sem dedup entre dias; cada contrato aparece só no dia da publicação.
- `204 No Content` = dia sem registros, não é erro.
- `totalPaginas` da primeira página define até onde paginar.
- Sem autenticação; header `accept: */*`.
- Delay entre requisições (padrão 0,5s) e retry com backoff exponencial em 429/5xx
  (`PNCP_REQUEST_DELAY`, `PNCP_MAX_RETRIES`, `PNCP_MAX_BACKOFF`). A API limita taxa com
  frequência: um dia de contratos (83 páginas) leva alguns minutos. Um dia que esgota as
  tentativas é registrado em `failed_days` e a varredura continua.
- `tamanhoPagina` precisa ficar entre 10 e 500 — a API devolve 400 fora disso.

## Filtros locais (antes de persistir)

O endpoint não filtra por assinatura, então o corte que define a amostra é local:

1. **Mantém se `dataAssinatura >= hoje - 365`**; descarta caso contrário. O critério é a
   data de assinatura, nunca `vigenciaFim` — vigência longa não torna o preço atual.
2. Registro **sem `dataAssinatura` é descartado**: sem ela não há como atestar a
   atualidade do preço.
3. Descarta `cancelado == true` ou `dataCancelamento` preenchida.
4. Contrato derivado de ata já coletada (por `numeroControlePncpAta` ou pela compra em
   comum): por padrão é **mantido e marcado**, não descartado. A marcação fica em
   `derived_from_ata` e `derivation_match` (`ata` = link explícito, `compra` = fallback).
   Para descartá-lo da amostra, `PNCP_DISCARD_DERIVED_CONTRACTS=true`.

Nas atas o descarte é pesado (o filtro da API é por vigência, então voltam atas
assinadas há 2–3 anos ainda vigentes). Nos contratos é menor, mas existe pela
defasagem entre assinatura e publicação — daí a janela de 400 dias.

**Suspeitos** (marcados em `suspicious` / `suspicious_reasons`, não descartados):
`vigenciaFim - vigenciaInicio > 730 dias` ou `vigenciaInicio < dataAssinatura`.

## Banco

- `atas` e `contratos`: colunas normalizadas + `raw` (JSONB com o payload original),
  chave primária `pncp_id`, gravação por *upsert* — reexecutar é idempotente.
- `collection_runs`: janela e estatísticas de cada execução (recebidos, duplicados,
  persistidos, suspeitos, descartes por motivo).

## Configuração

Tudo por variáveis de ambiente com prefixo `PNCP_` (ou `.env`), ver
[.env.example](.env.example): `DATABASE_URL`, `REQUEST_DELAY`, `MAX_RETRIES`,
`PAGE_SIZE`, `SIGNATURE_MAX_AGE_DAYS`, `ATAS_DAYS_BACK`, `ATAS_DAYS_AHEAD`,
`CONTRATOS_DAYS_BACK`, `SUSPICIOUS_VALIDITY_DAYS`, e os recortes opcionais `CNPJ` e
`CODIGO_UNIDADE_ADMINISTRATIVA`.

## Testes

```bash
uv run pytest
```

Cobrem os cortes de data (inclusive o limite exato de 365 dias), o dedup de atas entre
dias, o descarte de contrato derivado de ata, a marcação de suspeitos e a normalização
dos payloads. Não fazem rede nem exigem Postgres.

## Custo estimado

~455 dias de atas + 401 de contratos ≈ 856 chamadas mínimas, mais a paginação — as atas
paginam bastante nos dias mais antigos, já que toda ata vigente naquele dia é retornada.
