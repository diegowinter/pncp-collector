# pncp-collector

Coletor de **atas de registro de preços** e **contratos** do PNCP para pesquisa de preços,
com janela de 1 ano de assinatura. Persiste em Postgres, mostra progresso com `rich`.

Duas fases, independentes: a **fase 1** varre a API de consulta e grava as capas (atas e
contratos); a **fase 2** lê as capas do banco e busca, na API de detalhe, os arquivos, os
itens da compra e o resultado (preço homologado e fornecedor) de cada item.

## Instalação

```bash
uv sync
cp .env.example .env   # ajuste PNCP_DATABASE_URL
uv run pncp-collector init-db
```

## Uso

```bash
uv run pncp-collector atas          # fase 1: varredura por vigência
uv run pncp-collector contratos     # fase 1: varredura por publicação
uv run pncp-collector detalhar      # fase 2: arquivos, itens e resultados das capas no banco
uv run pncp-collector status        # quanto falta, por status
uv run pncp-collector all           # atas → contratos → detalhar (a ordem importa)
uv run pncp-collector all --reference 2026-09-08 --skip-detail -v
```

`all` roda as atas primeiro de propósito: o descarte de contrato derivado de ata
consulta as atas já persistidas. `--skip-detail` para só a fase 1.

Opções de `detalhar`:

| Opção | Efeito |
|---|---|
| `--tipo ata\|contrato\|all` | Só um dos datasets (padrão `all`). |
| `--status pendente,erro,...` | Sobrescreve a política de revisita. |
| `--limit N` | No máximo N documentos (passo A) e N compras (passos B e C). |
| `--refresh` | Revisita também documentos `ok` e refaz itens e resultados. |
| `--revisit-sem-arquivo` | Revisita documentos `sem_arquivo`. |
| `--only-step A\|B\|C\|D` | Executa só os passos indicados (`BC`, `D`...). |

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
- O progresso tem duas barras: **dias** (total fixo, a janela) e **páginas** (total parcial —
  quantas páginas um dia tem só se sabe ao ler a primeira, via `totalPaginas`, então o total
  cresce conforme os dias começam). A segunda existe porque um único dia pode ter dezenas de
  páginas, e sem ela a barra de dias parece travada.
- `PNCP_LIST_CONCURRENCY` (1–16, padrão 1) busca vários **dias** em paralelo. O ganho é
  esconder a latência, não acelerar o ritmo: `REQUEST_DELAY` passa a valer como intervalo
  mínimo entre chamadas somando todas as threads, e um 429 freia o grupo inteiro pelo
  `Retry-After` — sem isso, N threads só multiplicariam os 429. Os dias são entregues na
  ordem da janela, então dedup, gravação e estatísticas não dependem de quem respondeu
  primeiro, e o banco continua sendo escrito por uma thread só.
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

## Detalhamento (fase 2)

Lê as capas **do banco**, nunca da API, e roda em quatro passos sequenciais, cada um com
progresso e commit em lote — qualquer um pode ser interrompido e retomado:

| Passo | Unidade | O que faz |
|---|---|---|
| A | documento | Resolve a compra (`numero_controle_pncp_compra`; contrato sem ele usa `GET /orgaos/{cnpj}/contratos/{ano}/{seq}` como fallback), lista os arquivos e decide `sem_identificacao` / `sem_arquivo`. |
| B | compra | `GET .../compras/{ano}/{seq}/itens` (paginado; o default da API é 10, então pede 500). Grava **todos** os itens com a situação que vieram. |
| C | item `Homologado` com `temResultado` | `GET .../itens/{n}/resultados`. Grava **todos** os resultados e marca o vencedor (`is_winner`): menor `ordem_classificacao_srp` entre os de valor unitário > 0. Commit por compra: se cair no meio, a próxima execução refaz só os itens que faltaram. |
| D | só banco | Fecha o `detail_status` dos documentos a partir do estado da compra. |

A unidade dos passos B e C é a **compra**, não o documento: as N atas e contratos de um mesmo
pregão consultam itens e resultados uma vez só. Nada de PDF é baixado — só os identificadores
dos arquivos.

**Máquina de estados do documento** (`detail_status`), nesta ordem de precedência:

| Status | Quando |
|---|---|
| `sem_identificacao` | número de controle fora do formato, ou contrato sem compra mesmo após o fallback |
| `sem_arquivo` | a lista de arquivos não tem nenhum do tipo alvo (inclui 404 = documento removido) |
| `erro` | falha de rede/5xx esgotou as tentativas em qualquer chamada |
| `sem_homologado` | a compra não tem item `Homologado` (inclui compra sem itens) |
| `ok` | ≥ 1 item `Homologado` e resultados buscados |
| `pendente` | ainda não passou pelo passo A, ou a compra ainda não fechou (`results_status = parcial`) |

**Arquivo alvo:** para atas, **qualquer arquivo** conta — no órgão medido, 100 % dos arquivos
de ata vêm como `Outros Documentos`, com a ata em si no meio (`ATA DIGITALIZADA.pdf`). Para
contratos, `Contrato` ou `Nota de Empenho` (o instrumento do contrato-por-empenho). Ambos
configuráveis por `PNCP_TARGET_FILE_TYPES_ATA` / `_CONTRATO`.

**Política de revisita** (o que `detalhar` volta a olhar sem flag):

| Status | Revisita? | Por quê |
|---|---|---|
| `pendente` | sim | ainda não foi, ou a compra não fechou |
| `erro` | até `PNCP_DETAIL_MAX_ATTEMPTS` | transitório |
| `sem_homologado` | sim | a homologação pode ter saído depois; a compra é reconsultada |
| `sem_arquivo` | só com `--revisit-sem-arquivo` | raro mudar |
| `sem_identificacao` | não | a capa não muda sem nova fase 1 |
| `ok` | só com `--refresh` | já tem tudo |

**Custo e ritmo.** A API de detalhe é lenta (~8 s por chamada) mas não limita taxa — o oposto
da de consulta. Chamadas ≈ documentos (arquivos) + compras distintas (itens) + itens
homologados com resultado. Para a base medida (891 capas, 752 compras): ~6.450 chamadas,
~15 h em sequência ou ~2 h com `PNCP_DETAIL_CONCURRENCY=8`. Números e payloads reais em
[docs/API_DETALHE.md](docs/API_DETALHE.md); queries de leitura em
[docs/CONSULTAS.md](docs/CONSULTAS.md).

## Banco

**Convenção de nomes:** as colunas que vêm do PNCP mantêm o nome da API, apenas
convertido de camelCase para snake_case — `dataAssinatura` → `data_assinatura`,
`numeroControlePNCPAta` → `numero_controle_pncp_ata`. Objetos aninhados são achatados
carregando o caminho no nome: `orgaoEntidade.razaoSocial` → `orgao_entidade_razao_social`.
Os valores vão como vieram; a única transformação é no nome. Só os campos de controle
nossos ficam em inglês: `collected_at`, `updated_at`, `raw`, `suspicious`,
`suspicious_reasons`, `derived_from_ata`, `derivation_match` e a tabela
`collection_runs` inteira.

Como os nomes diferem entre os dois datasets (a ata tem `vigencia_inicio`, o contrato tem
`data_vigencia_inicio`), os filtros locais leem os campos por um mapa declarado em
`filters.DatasetFields`.

- `atas` e `contratos`: colunas espelhando a API + `raw` (JSONB com o payload íntegro),
  chave primária `numero_controle_pncp_ata` e `numero_controle_pncp` respectivamente,
  gravação por *upsert* — reexecutar é idempotente.
- Contratos não têm `cancelado`/`data_cancelamento`: a API não devolve esses campos para
  contratos, então o filtro de cancelamento vale só para atas.
- O que não virou coluna continua acessível em `raw` (ex.: `frutoAdesao`,
  `identificadorCipi`, dados de subcontratação).
- `collection_runs`: janela e estatísticas de cada execução (recebidos, duplicados,
  persistidos, suspeitos, descartes por motivo). A fase 2 grava com `dataset = 'detalhe'`.

Fase 2:

- `atas` e `contratos` ganham o estado do detalhamento: `detail_status`, `detail_error`,
  `detailed_at`, `detail_attempts`, `compra_key` (FK lógica para `compras`); contratos também
  `compra_resolved_by` (`capa` ou `fallback`).
- `compras`: uma linha por compra (`compra_key` = `numero_controle_pncp_compra`), com `cnpj`,
  `ano`, `sequencial`, `url_pncp` (página pública, para conferência manual), `items_status` /
  `results_status` separados (resultados são N chamadas e podem parar no meio), totais e
  tentativas. Sem `raw`: não existe payload "da compra", só o dos itens.
- `itens`: PK `(compra_key, numero_item)`; colunas espelhando a API (`situacao_compra_item`
  e `situacao_compra_item_nome`, `tem_resultado`, `valor_unitario_estimado`...) + `raw`.
- `itens_resultados`: PK `(compra_key, numero_item, sequencial_resultado)`; todos os
  resultados do item + `is_winner` (controle nosso) + `raw`.
- `arquivos`: PK `(documento_tipo, documento_id, sequencial_documento)`; `tipo_documento_nome`,
  `titulo`, `url`, `data_publicacao_pncp` + `is_target_type` + `raw`. Arquivo removido do PNCP
  não é apagado daqui; fica com `updated_at` antigo.
- Banco já existente: [docs/MIGRACAO_DETALHE.sql](docs/MIGRACAO_DETALHE.sql) acrescenta as
  colunas nas capas (as tabelas novas o `init-db` cria).

## Configuração

Tudo por variáveis de ambiente com prefixo `PNCP_` (ou `.env`), ver
[.env.example](.env.example): `DATABASE_URL`, `REQUEST_DELAY`, `MAX_RETRIES`,
`PAGE_SIZE`, `SIGNATURE_MAX_AGE_DAYS`, `ATAS_DAYS_BACK`, `ATAS_DAYS_AHEAD`,
`CONTRATOS_DAYS_BACK`, `SUSPICIOUS_VALIDITY_DAYS`, e os recortes opcionais `CNPJ` e
`CODIGO_UNIDADE_ADMINISTRATIVA` (o `CNPJ` também recorta a fase 2).

Fase 1: `PAGE_SIZE` (10–500, padrão 100 — subir para 500 é o corte mais direto no número
de requisições) e `LIST_CONCURRENCY` (1–16 dias em paralelo, padrão 1).

Fase 2: `DETAIL_CONCURRENCY` (1–8 threads), `DETAIL_REQUEST_DELAY` (padrão = `REQUEST_DELAY`),
`DETAIL_MAX_ATTEMPTS`, `DETAIL_PAGE_SIZE` e `TARGET_FILE_TYPES_ATA` / `TARGET_FILE_TYPES_CONTRATO`.

## Testes

```bash
uv run pytest
```

Cobrem os cortes de data (inclusive o limite exato de 365 dias), o dedup de atas entre
dias, o descarte de contrato derivado de ata, a marcação de suspeitos e a normalização
dos payloads. Na fase 2: parse dos números de controle, o cliente de detalhe com transporte
falso (204/404/429/400), as regras puras (arquivo alvo, vencedor, máquina de estados) e a
orquestração com cliente e repositório em memória (compra compartilhada consulta itens uma
vez, fallback do contrato, retomada após erro parcial, política de revisita). Não fazem rede
nem exigem Postgres.

## Custo estimado

Fase 1: ~455 dias de atas + 401 de contratos ≈ 856 chamadas mínimas, mais a paginação — as
atas paginam bastante nos dias mais antigos, já que toda ata vigente naquele dia é retornada.
`LIST_CONCURRENCY` corta o tempo ocioso enquanto se espera a API, mas o teto continua sendo
1/`REQUEST_DELAY` chamadas por segundo; quanto essa API aguenta antes de responder 429 ainda
não foi medido (ver docs/API_DETALHE.md, § 1.6, que mediu só a de detalhe).

Fase 2 (medido em 2026-09-15 para um órgão com 891 capas): ~6.450 chamadas a ~8 s cada.
Ver § Detalhamento e [docs/API_DETALHE.md](docs/API_DETALHE.md).

## Convenção de commits

Um commit por atividade, no formato:

```
<tipo>(<escopo>): <resumo no imperativo, minúsculo, sem ponto final>

<corpo: o quê e por quê>

Plano: <documento> § <atividade>      (quando o commit executa um plano)
```

`tipo` ∈ `feat`, `fix`, `docs`, `test`, `refactor`, `chore`; `escopo` é o módulo tocado
(`client`, `models`, `detail`, `cli`, `docs`...).
