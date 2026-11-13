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
uv run pncp-collector triar         # fase 3: triagem das compras pelo objeto (modelo de decisões)
uv run pncp-collector detalhar --triagem potencial   # fase 2 só nas compras que a triagem apontou
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
| `--triagem potencial[,indefinido]` | Só capas cuja compra está nesses `triage_status` (fase 3). Sem a opção, todas, como antes. |

Opções de `triar`:

| Opção | Efeito |
|---|---|
| `--status pendente,erro,...` | Quais `triage_status` (re)triar (padrão `pendente,erro`). |
| `--limit N` | No máximo N compras nesta execução. |
| `--no-seed` / `--only-seed` | Pula, ou faz só, a criação das compras a partir das capas. |

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
  frequência: um dia de contratos (83 páginas) leva alguns minutos. Sem `PNCP_MAX_RETRIES`
  as tentativas são infinitas; com ele, um dia que esgota as tentativas é registrado em
  `failed_days` e a varredura continua.
- **Gravação por página:** cada thread busca um dia (`LIST_CONCURRENCY` dias em paralelo) e
  entrega página a página numa fila limitada; só a thread principal, com uma única conexão,
  filtra e grava. Se a gravação atrasa, quem baixa espera, então a memória fica em poucas
  páginas, não em dias inteiros.
- **Memória:** a cada 20 páginas gravadas (e a cada lote da fase 2) o coletor chama
  `malloc_trim(0)` da glibc. Sem isso, no Linux, o RSS sobe ~0,4 MB por página até o OOM
  killer, mesmo com o heap do Python estável: as threads alocam e liberam blocos grandes em
  arenas diferentes e a glibc não devolve as páginas livres ao kernel. Medido na TV box de
  1 GB: 90 MB estáveis contra mais de 600 MB. `scripts/memprobe.py` roda a CLI com uma sonda
  que loga RSS, pymalloc e glibc, para medir de novo se precisar.
- **Conexão com o banco:** cada commit (página, fim de dia, execução) é uma transação curta
  e própria; não há sessão aberta durante a varredura inteira. Isso importa com Postgres
  remoto: a thread principal passa minutos sem tocar o banco enquanto espera a API, e um
  NAT/firewall derruba conexão ociosa sem avisar (`could not receive data from server:
  Connection timed out`). O engine ainda liga keepalive TCP (sonda a cada 60 s) e recicla
  conexões a cada 30 min; se mesmo assim a conexão cair, a transação é repetida com backoff
  até 8 vezes antes de abortar. Como os upserts são idempotentes, repetir é seguro.
- **Retomada:** cada commit leva os registros da página e a última página gravada do dia
  (`collection_day_progress`); dia completo vai para `collection_days`. Sem `--reference`,
  `atas`, `contratos` e `all` retomam a última varredura incompleta: pulam os dias feitos e
  continuam os dias em andamento da página seguinte. Dias com falha ficam para a próxima
  execução. Rodar a fase 1 de novo não mexe no estado da fase 2 das capas.
- **Ctrl+C** encerra na hora: as chamadas em retry desistem, em vez de esperar os dias (ou
  o bloco da fase 2) que estavam em andamento.
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
| `erro` | sempre, ou até `PNCP_DETAIL_MAX_ATTEMPTS` se definido | transitório |
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

## Triagem por objeto (fase 3)

O lake tem centenas de milhares de atas, a maioria de saúde, merenda e obras. Detalhar tudo
(fase 2) é caro, então antes se decide **pela capa** quais compras valem a pena. A unidade é a
compra: as N atas de um mesmo pregão trazem o mesmo objeto, e a fase 2 já é por compra.

Dois passos, ambos retomáveis:

| Passo | O que faz |
|---|---|
| semear | Cria a linha em `compras` a partir das capas, com **preenchimento parcial**: só a chave (e o que se deriva dela) e o `objeto`, copiado da ata ou do contrato (`objeto_source`). Nenhum outro campo da capa é copiado, porque podem divergir entre documentos da mesma compra. Compra que já existia (da fase 2) só ganha o objeto. Sem API. |
| triar | Uma chamada por compra pendente ao **endpoint de decisões** do OpenRouter (`POST /api/alpha/decisions`, modelo `typesafe/jev-1.14`). Não é chat: vão perguntas tipadas e voltam probabilidades. |

Perguntas feitas (definidas em `triage_rules.py`, mesmas regras de "entra / não entra" do
classificador de itens do pesquisa-de-precos-pncp):

- `clareza` (`claro` / `generico` / `remete_ao_edital`): o objeto nomeia o que é fornecido?
- `potencial` (probabilidade): pode haver item de interesse para a pesquisa de preços de
  segurança pública? Pergunta pela **possibilidade**: objeto amplo conta como possível.
- um `noul` por grupo: `armamento`, `vestuario_operacional`, `viaturas`, `ti_comunicacao`,
  `drone_bicicleta`, `servicos`. Cada grupo agrupa categorias de item (ex.: `armamento` =
  `arma_fogo`, `arma_nao_letal`, `municao`, `protecao_balistica`).

Decisão (`triage_status`), em ordem:

1. `potencial` ≥ `PNCP_TRIAGE_POTENTIAL_THRESHOLD` (0,5) → **`potencial`**, mesmo com objeto
   genérico: a fase 2 vai dizer o que há dentro.
2. objeto `generico` ou `remete_ao_edital` → **`indefinido`**: não dá para decidir pela capa.
   Fica na fila para classificar pelos PDFs (OCR dos arquivos; a fazer).
3. senão → **`fora_por_ora`**.

Nada é descartado. As probabilidades (`triage_potential`, `triage_groups`) e a resposta crua
(`triage_raw`) ficam na linha, então dá para reclassificar mudando o limiar sem nova chamada,
ou retriar com outro modelo (`triar --status fora_por_ora,indefinido,potencial`). Contrato
derivado de ata herda a decisão: ela é da compra, não do documento.

O critério da capa é **recall**: "equipamentos de informática" pode ser notebook ou toner, e
só o item diz. Por isso em dúvida a compra vai para `potencial` e o corte fino continua no
classificador de itens.

Medido em 2026-11-13 (amostra de 200 compras + 40 pares ata/contrato): ~US$ 0,000045 e 0,6 s
por compra com 8 chamadas em paralelo; o lote de 125 mil compras vigentes sai por ~US$ 6 em
~3 h. `PNCP_TRIAGE_MAX_COST_USD` põe um teto por execução. A validação humana está em
[docs/validacao/](docs/validacao/) (planilha rotulada à mão + previsões do modelo à parte).

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

Fase 3:

- `compras` ganha `objeto` e `objeto_source` (preenchimento parcial pela capa) e o estado da
  triagem: `triage_status`, `triage_clarity`, `triage_potential`, `triage_groups`
  (`{grupo: probabilidade}`), `triage_model`, `triage_error`, `triage_attempts`, `triaged_at`,
  `triage_raw`. Os **valores** de classificação ficam em português (`potencial`, `armamento`...);
  só os nomes das colunas são em inglês, como os demais campos de controle.
- `collection_runs` recebe `dataset = 'triagem'` com chamadas, custo em dólares e tokens.
- Banco já existente: [docs/MIGRACAO_TRIAGEM.sql](docs/MIGRACAO_TRIAGEM.sql) (idempotente).
- Banco criado antes de `processo`/`numero_contrato_empenho`/`numero_ata_registro_preco`
  virarem `TEXT`: [docs/MIGRACAO_TEXTO_LIVRE.sql](docs/MIGRACAO_TEXTO_LIVRE.sql)
  (so metadado, roda em segundos). Sem ela um valor acima de 120 caracteres dá
  `DataError` e derruba a varredura na mesma página a cada retomada.
- Banco criado antes de `ni_fornecedor` virar `TEXT`: fornecedor estrangeiro chega como
  `ESTRANGEIRO_<NOME>` e era cortado em 30 caracteres. `uv run python
  scripts/patch_ni_fornecedor.py` aplica [docs/MIGRACAO_NI_FORNECEDOR.sql](docs/MIGRACAO_NI_FORNECEDOR.sql)
  e refaz o campo a partir de `raw`, sem chamar a API (`--dry-run` só conta as linhas).

## Configuração

Tudo por variáveis de ambiente com prefixo `PNCP_` (ou `.env`), ver
[.env.example](.env.example): `DATABASE_URL`, `REQUEST_DELAY`, `MAX_RETRIES`,
`PAGE_SIZE`, `SIGNATURE_MAX_AGE_DAYS`, `ATAS_DAYS_BACK`, `ATAS_DAYS_AHEAD`,
`CONTRATOS_DAYS_BACK`, `SUSPICIOUS_VALIDITY_DAYS`, e os recortes opcionais `CNPJ` e
`CODIGO_UNIDADE_ADMINISTRATIVA` (o `CNPJ` também recorta a fase 2). `MAX_RETRIES` e
`DETAIL_MAX_ATTEMPTS` não têm limite quando não definidos.

Fase 1: `PAGE_SIZE` (10–500, padrão 100 — subir para 500 é o corte mais direto no número
de requisições) e `LIST_CONCURRENCY` (1–16 dias em paralelo, padrão 1).

Fase 2: `DETAIL_CONCURRENCY` (1–8 threads), `DETAIL_REQUEST_DELAY` (padrão = `REQUEST_DELAY`),
`DETAIL_MAX_ATTEMPTS`, `DETAIL_PAGE_SIZE` e `TARGET_FILE_TYPES_ATA` / `TARGET_FILE_TYPES_CONTRATO`.

Fase 3: `OPENROUTER_API_KEY` (obrigatória para `triar`; nunca no repositório),
`TRIAGE_MODEL` (padrão `typesafe/jev-1.14`), `TRIAGE_CONCURRENCY` (1–32, padrão 8),
`TRIAGE_POTENTIAL_THRESHOLD` e `TRIAGE_GROUP_THRESHOLD` (padrão 0,5), `TRIAGE_MAX_ATTEMPTS`
e `TRIAGE_MAX_COST_USD` (teto de gasto por execução; sem limite quando não definidos).

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

Fase 3: ~US$ 0,000045 por compra no `typesafe/jev-1.14` (medido em 2026-11-13); ver
§ Triagem por objeto. É a fase mais barata e a que decide quanto a fase 2 vai custar.

## Convenção de commits

Um commit por atividade, no formato:

```
<tipo>(<escopo>): <resumo no imperativo, minúsculo, sem ponto final>

<corpo: o quê e por quê>

Plano: <documento> § <atividade>      (quando o commit executa um plano)
```

`tipo` ∈ `feat`, `fix`, `docs`, `test`, `refactor`, `chore`; `escopo` é o módulo tocado
(`client`, `models`, `detail`, `cli`, `docs`...).
