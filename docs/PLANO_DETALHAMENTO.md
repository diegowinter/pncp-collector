# Plano: detalhamento de atas e contratos (arquivos, itens e resultados)

Atividades para implementar, neste coletor, a "etapa 2" descrita em
[COLETA_PNCP.md](COLETA_PNCP.md) — **sem a busca textual**. Aqui a descoberta já é feita pela
varredura por vigência/publicação (`atas` e `contratos`); o que falta é, para cada capa
persistida, buscar arquivos, itens da compra e resultado vencedor.

Escrito em 2026-09-15. O código atual é a base: [pipeline.py](../src/pncp_collector/pipeline.py),
[client.py](../src/pncp_collector/client.py), [models.py](../src/pncp_collector/models.py),
[db.py](../src/pncp_collector/db.py), [filters.py](../src/pncp_collector/filters.py).

---

## 0. Premissas e decisões de desenho

Fixadas antes de começar, para não serem rediscutidas atividade por atividade.

| # | Decisão | Motivo |
|---|---|---|
| D1 | **Duas fases sequenciais e independentes**: fase 1 = capas (o que já existe); fase 2 = detalhamento, que lê as capas **do banco**, nunca da API. | É o que foi pedido. Também permite rodar a fase 2 quantas vezes for preciso sem refazer a varredura. |
| D2 | **Nada de busca textual, termos, `documento_termo`, `conceitos_origem`, watermark por termo.** | Não existem nesta abordagem; a amostra é "tudo assinado no último ano". |
| D3 | **A unidade da fase 2 é a compra**, não a ata nem o contrato (ADR-024 do outro pipeline). Itens e resultados são consultados **uma vez por compra** e ligados a N documentos. | Evita as 2.200+ chamadas do caso Embrapa. Também é o que permite o vínculo ata ↔ contrato pela compra, que já usamos em `derivation_match`. |
| D4 | **A chave da compra sai do campo que a capa já traz**: `numero_controle_pncp_compra` (atas e contratos). A chamada extra `/orgaos/{cnpj}/contratos/{ano}/{seq}` é só **fallback** para contrato com o campo vazio. | Diferente do outro pipeline, que partia do resultado da busca. Aqui a capa da API de consulta já carrega o número de controle da compra. |
| D5 | **Não baixa PDF.** Só grava os identificadores dos arquivos. | ADR-011 do outro pipeline. Download é outra etapa, se um dia existir. |
| D6 | **Guarda todos os resultados de cada item**, não só o vencedor, e marca o vencedor numa coluna. | Resolve a pendência "só um vencedor por item" do outro pipeline; custa zero chamadas a mais (a lista já vem inteira). |
| D7 | **Só item `Homologado` conta para o status `ok`**, mas **todos os itens da compra são gravados** com a situação que vieram. | Regravar sem refazer chamadas se a regra mudar; e o item cancelado/deserto tem valor de diagnóstico. |
| D8 | **Convenção de nomes mantida**: coluna que vem do PNCP é o nome da API em snake_case; controle nosso em inglês (`detail_status`, `detail_error`, `detailed_at`, `raw`, ...). | Regra do README, § Banco. |
| D9 | **Estado de detalhamento persiste por documento e por compra**, para retomar de onde parou e revisitar pendentes. | Um `all` nacional leva horas; interrupção é o caso normal, não a exceção. |
| D10 | **Contratos derivados de ata** (`derived_from_ata = true`) **também são detalhados** por padrão, mas como a compra é a mesma, o custo extra é só a chamada de arquivos. | Consistente com "manter e marcar" (pendência aberta no README). |

Termos usados abaixo:

- **capa** — linha em `atas` ou `contratos`, resultado da fase 1.
- **documento** — uma capa (ata ou contrato) sob o ponto de vista da fase 2.
- **compra** — o pregão/contratação de onde saem os itens; identificada por `(cnpj, ano, sequencial)`.

---

## 1. Verificações na API real (antes de codar)

Tudo que o outro pipeline dá por certo precisa ser confirmado aqui, porque a origem dos dados
é diferente (API de consulta, não a busca). Resultado de cada item vai para
`docs/API_DETALHE.md` (novo), com exemplo de payload real de cada endpoint.

### 1.1 Formato dos números de controle

- [ ] Pegar ~20 atas e ~20 contratos já no banco (`SELECT raw FROM atas LIMIT 20`) e confirmar:
  - `numeroControlePNCPCompra` segue `{cnpj:14}-1-{seq:6}/{ano:4}` em **todos** os casos;
  - `numeroControlePNCPAta` segue `{numeroControlePNCPCompra}-{seqAta:6}` (sufixo é o sequencial da ata);
  - `numeroControlePNCP` do contrato segue `{cnpj}-2-{seq}/{ano}` (o `2` é o tipo "contrato"; o `seq` é do **contrato**, não da compra).
- [ ] Medir quantos contratos têm `numero_controle_pncp_compra` **nulo/vazio** (é isso que decide quão importante é o fallback D4):
  ```sql
  SELECT count(*) FILTER (WHERE numero_controle_pncp_compra IS NULL OR numero_controle_pncp_compra = '') AS sem_compra,
         count(*) AS total
  FROM contratos;
  ```
- [ ] Mesma medição para atas (esperado: zero; se não for, registrar).
- [ ] Registrar se aparece `numeroControlePNCPCompra` com formato diferente (ex.: sem zeros à esquerda, com espaço). O parser da atividade 3 precisa dos casos reais.

### 1.2 Endpoint de arquivos

- [ ] Chamar, para 5 atas e 5 contratos reais:
  - `GET /api/pncp/v1/orgaos/{cnpj}/compras/{ano}/{seq}/atas/{seqAta}/arquivos`
  - `GET /api/pncp/v1/orgaos/{cnpj}/contratos/{ano}/{seq}/arquivos`
- [ ] Anotar: formato da resposta (lista direta ou envelope), campos por arquivo (`sequencialDocumento`, `tipoDocumentoId`, `tipoDocumentoNome`, `titulo`, `url`, `uri`, `dataPublicacaoPncp`, `statusAtivo`...), status quando não há arquivo (200 com `[]`? 204? 404?).
- [ ] Confirmar os valores exatos de `tipoDocumentoNome` alvo: `"Contrato"` e `"Ata de Registro de Preço"` (com acento? "Preços"?). Coletar a lista de valores distintos vistos.
- [ ] Verificar se arquivo inativo (`statusAtivo = false`) aparece na lista e se deve contar.

### 1.3 Endpoint de itens

- [ ] `GET /api/pncp/v1/orgaos/{cnpj}/compras/{ano}/{seq}/itens` — anotar: paginação (`pagina`, `tamanhoPagina`? limite?), campos (`numeroItem`, `descricao`, `unidadeMedida`, `quantidade`, `valorUnitarioEstimado`, `valorTotal`, `situacaoCompraItemId`, `situacaoCompraItemNome`, `temResultado`, `materialOuServico`, `itemCategoriaNome`, `criterioJulgamentoNome`, `ncmNbsCodigo`...).
- [ ] `GET .../itens/quantidade` — confirmar se devolve inteiro puro e se vale a pena (só serve para saber quantas páginas pedir; se `/itens` sem paginação já traz tudo, dispensar).
- [ ] Testar compra com muitos itens (>500) para ver se a resposta trunca.
- [ ] Coletar a lista de valores distintos de `situacaoCompraItemNome` (esperado: Homologado, Cancelado, Deserto, Fracassado, Em Andamento, Anulado/Revogado/Suspenso).

### 1.4 Endpoint de resultados

- [ ] `GET .../itens/{numeroItem}/resultados` — anotar: lista ou objeto único; campos (`valorUnitarioHomologado`, `valorTotalHomologado`, `quantidadeHomologada`, `ordemClassificacaoSrp`, `niFornecedor`, `nomeRazaoSocialFornecedor`, `tipoPessoa`, `porteFornecedorNome`, `dataResultado`, `situacaoCompraItemResultadoNome`, `sequencialResultado`, `indicadorSubcontratacao`, `aplicacaoMargemPreferencia`...).
- [ ] Verificar comportamento para item com `temResultado = false` (200 `[]`? 404?).
- [ ] Verificar item com **mais de um** resultado (SRP com cadastro de reserva): confirmar que `ordemClassificacaoSrp` é o critério de vencedor e como vem para não-SRP (null? 1?).
- [ ] Verificar se existe endpoint agregado de resultados por compra (`.../compras/{ano}/{seq}/resultados` ou similar). Se existir, é a maior economia possível: 1 chamada em vez de N por compra. **Registrar o resultado mesmo que negativo.**

### 1.5 Fallback do contrato

- [ ] `GET /api/pncp/v1/orgaos/{cnpj}/contratos/{ano}/{seq}` — confirmar que devolve `numeroControlePncpCompra` e o que mais devolve (pode ter campos que a consulta não traz).

### 1.6 Ritmo

- [ ] Medir latência média e frequência de 429 em `/api/pncp/v1/*` com o delay atual (0,5 s). Os endpoints de detalhe podem ter limite diferente dos de consulta.
- [ ] Estimar, com os números do banco atual, o total de chamadas:
  ```
  chamadas ≈ docs × 1 (arquivos)
           + contratos_sem_compra × 1 (fallback)
           + compras_distintas × (1 itens [+ páginas])
           + itens_com_resultado × 1 (resultados)
  ```
  Registrar em `docs/API_DETALHE.md` com a data. Esse número decide se `concurrency` (atividade 7.4) é opcional ou obrigatório.

**Critério de conclusão:** `docs/API_DETALHE.md` existe, com payload de exemplo de cada endpoint e resposta a cada pergunta acima. Nada das atividades 2+ começa sem isso.

---

## 2. Esquema do banco

Arquivo: [models.py](../src/pncp_collector/models.py). Tudo via `Base.metadata.create_all` (não
há Alembic; para banco existente, `reset-db` ou `ALTER TABLE` manual — ver 2.6).

### 2.1 Estado de detalhamento nas capas

Adicionar a `Ata` e a `Contrato` (via um novo `DetailMixin`, ao lado de `ControlMixin`):

| Coluna | Tipo | Significado |
|---|---|---|
| `detail_status` | `String(30)`, index, default `'pendente'` | `pendente` / `ok` / `sem_identificacao` / `sem_arquivo` / `sem_homologado` / `erro` |
| `detail_error` | `Text`, nullable | Mensagem da última exceção (só quando `erro`) |
| `detailed_at` | `DateTime(tz)`, nullable | Quando o status foi decidido pela última vez |
| `detail_attempts` | `Integer`, default 0 | Quantas vezes a fase 2 tentou este documento |
| `compra_key` | `String(80)`, index, nullable | FK lógica para `compras.compra_key`; preenchida na resolução (atividade 7.2) |

Só `Contrato`:

| Coluna | Tipo | Significado |
|---|---|---|
| `compra_resolved_by` | `String(20)`, nullable | `capa` (veio de `numero_controle_pncp_compra`) ou `fallback` (chamada extra) |

### 2.2 Tabela `compras`

Uma linha por compra; é o cache da D3.

| Coluna | Tipo | Origem |
|---|---|---|
| `compra_key` | `String(80)`, PK | `= numero_controle_pncp_compra` da capa (`00348003000110-1-000507/2025`) |
| `cnpj` | `String(14)`, index | parse da chave |
| `ano` | `Integer` | parse da chave |
| `sequencial` | `Integer` | parse da chave |
| `url_pncp` | `Text` | URL pública reconstruída (atividade 3.2), para inspeção manual |
| `items_status` | `String(30)`, index | `pendente` / `ok` / `sem_itens` / `erro` |
| `items_error` | `Text`, nullable | |
| `items_fetched_at` | `DateTime(tz)`, nullable | |
| `total_itens` | `Integer`, nullable | tamanho da lista devolvida |
| `total_homologados` | `Integer`, nullable | contagem local |
| `results_status` | `String(30)`, index | `pendente` / `ok` / `parcial` / `erro` — separado de `items_status` porque resultados são N chamadas e podem falhar no meio |
| `results_fetched_at` | `DateTime(tz)`, nullable | |
| `collected_at`, `updated_at` | | como no `ControlMixin` |

Sem `raw` (não há um payload "da compra"; o que há são os itens).

### 2.3 Tabela `itens`

Uma linha por item de compra. PK composta `(compra_key, numero_item)`.

Colunas do PNCP (snake_case do que a 1.3 confirmar), no mínimo: `numero_item`, `descricao`,
`material_ou_servico`, `unidade_medida`, `quantidade` (`Numeric(18,4)`), `valor_unitario_estimado`,
`valor_total` (`Numeric(18,4)`), `situacao_compra_item_id`, `situacao_compra_item_nome` (index),
`tem_resultado` (`Boolean`), `criterio_julgamento_nome`, `item_categoria_nome`, `ncm_nbs_codigo`,
`data_inclusao`, `data_atualizacao`.

Controle: `raw` (JSONB, payload íntegro), `collected_at`, `updated_at`.

Índice: `(situacao_compra_item_nome, compra_key)` para as consultas "itens homologados da compra".

### 2.4 Tabela `itens_resultados`

Uma linha por resultado (D6). PK composta `(compra_key, numero_item, sequencial_resultado)`; se
a 1.4 mostrar que `sequencialResultado` não existe ou não é único, usar `ordem_classificacao_srp`
com fallback para posição na lista — decidir na atividade 1.

Colunas do PNCP, no mínimo: `sequencial_resultado`, `ordem_classificacao_srp`, `ni_fornecedor`
(index), `nome_razao_social_fornecedor`, `tipo_pessoa`, `porte_fornecedor_nome`,
`quantidade_homologada`, `valor_unitario_homologado` (`Numeric(18,4)`), `valor_total_homologado`,
`percentual_desconto`, `situacao_compra_item_resultado_nome`, `data_resultado` (`Date`),
`indicador_subcontratacao`, `aplicacao_margem_preferencia`.

Controle: `is_winner` (`Boolean`, index) — o vencedor segundo a regra de preço da atividade 6.3;
`raw`, `collected_at`, `updated_at`.

### 2.5 Tabela `arquivos`

Uma linha por arquivo listado, de ata ou de contrato. Não é por compra: arquivos pertencem ao
documento.

| Coluna | Tipo | |
|---|---|---|
| `documento_tipo` | `String(10)`, PK | `ata` / `contrato` |
| `documento_id` | `String(80)`, PK, index | `numero_controle_pncp_ata` ou `numero_controle_pncp` |
| `sequencial_documento` | `Integer`, PK | da API |
| `tipo_documento_id`, `tipo_documento_nome` (index), `titulo`, `url`, `data_publicacao_pncp`, `status_ativo` | | conforme 1.2 |
| `is_target_type` | `Boolean`, index | controle nosso: é do tipo que conta para `sem_arquivo`? |
| `raw`, `collected_at`, `updated_at` | | |

### 2.6 Migração do banco existente

- [ ] Escrever `docs/MIGRACAO_DETALHE.sql` com os `ALTER TABLE atas/contratos ADD COLUMN ...` (as tabelas novas o `create_all` cria sozinho).
- [ ] Alternativa documentada: `reset-db --yes` + `all` (perde a fase 1; só aceitável enquanto for base de teste).
- [ ] Atualizar `schemas.CONTROL_COLUMNS` com todas as colunas de controle novas — senão `normalize` tenta preenchê-las do payload.

### 2.7 Critério de conclusão

- `uv run pncp-collector init-db` cria as 4 tabelas novas + colunas.
- `uv run pytest` continua verde (os testes de normalização não podem quebrar pelas colunas novas).

---

## 3. Identificadores: parse e montagem de URLs

Novo módulo `src/pncp_collector/ids.py`. Puro, sem rede, 100% testável.

### 3.1 Parse

- [ ] `parse_compra_key(value: str) -> CompraRef | None`, onde `CompraRef = (cnpj: str, ano: int, sequencial: int, key: str)`. Regex a partir do que a 1.1 confirmar; devolve `None` (nunca exceção) para formato inválido. `key` é o valor original normalizado (strip).
- [ ] `parse_ata_key(value: str) -> AtaRef | None` com `AtaRef = (compra: CompraRef, sequencial_ata: int, key: str)`. Deriva do sufixo `-{seqAta}`.
- [ ] `parse_contrato_key(value: str) -> ContratoRef | None` com `(cnpj, ano, sequencial_contrato, key)`. Lembrar: o sequencial aqui é do **contrato**; nunca usar como sequencial de compra.

### 3.2 URLs

- [ ] `url_arquivos_ata(ref: AtaRef) -> str`
- [ ] `url_arquivos_contrato(ref: ContratoRef) -> str`
- [ ] `url_itens(ref: CompraRef) -> str`, `url_itens_quantidade(ref)` (se a 1.3 mostrar utilidade)
- [ ] `url_resultados(ref: CompraRef, numero_item: int) -> str`
- [ ] `url_contrato_detalhe(ref: ContratoRef) -> str` (fallback D4)
- [ ] `url_publica_compra(ref: CompraRef) -> str` = `https://pncp.gov.br/app/editais/{cnpj}/{ano}/{seq}` — gravado em `compras.url_pncp`.

### 3.3 Testes (`tests/test_ids.py`)

- [ ] Parse dos formatos reais colhidos na 1.1 (usar strings reais, não inventadas).
- [ ] Casos inválidos: vazio, `None`, sem barra, sem sufixo de ata, CNPJ com menos de 14 dígitos.
- [ ] Contrato `9-2-3/2026` **não** vira compra.
- [ ] URLs batem caractere a caractere com os exemplos de `docs/API_DETALHE.md`.

---

## 4. Cliente HTTP: endpoints de detalhe

Arquivo: [client.py](../src/pncp_collector/client.py).

### 4.1 Generalizar `_get`

Hoje `_get` é feito para a consulta por dia: 204 → `None`, 4xx → `raise_for_status`, esgotou →
`DayFetchError`. Para o detalhe precisamos distinguir "não existe" de "falhou".

- [ ] Extrair `_request(path, params) -> httpx.Response` com o retry/backoff atual, mas **sem** interpretar 204/404.
- [ ] Manter `_get` com o comportamento de hoje, por cima de `_request` (nada da fase 1 muda).
- [ ] Nova exceção `DetailFetchError(RuntimeError)` com atributos `status_code: int | None` e `path: str`. Distinguir:
  - `DetailNotFound(DetailFetchError)` para 404/410: recurso removido;
  - 4xx restantes: terminal, `DetailFetchError`, **sem** retry;
  - esgotou retries em 429/5xx/transporte: `DetailFetchError` com `status_code` do último.
- [ ] `time.sleep(self.config.request_delay)` após **toda** chamada de detalhe, igual à consulta. Se a 1.6 mostrar limite diferente, nova config `detail_request_delay` (default = `request_delay`).

### 4.2 Métodos novos

Todos devolvem o JSON já decodificado; a normalização é do módulo `schemas`.

- [ ] `get_arquivos_ata(ref: AtaRef) -> list[dict]`
- [ ] `get_arquivos_contrato(ref: ContratoRef) -> list[dict]`
- [ ] `get_itens(ref: CompraRef) -> list[dict]` — pagina internamente se a 1.3 mostrar paginação; devolve a lista completa.
- [ ] `get_resultados(ref: CompraRef, numero_item: int) -> list[dict]` — normaliza para lista mesmo que a API devolva objeto único; `DetailNotFound` vira `[]` **aqui** (item sem resultado não é erro).
- [ ] `get_contrato(ref: ContratoRef) -> dict` (fallback D4).
- [ ] Em cada um: 204 e `[]` são equivalentes a "vazio"; 404 em arquivos/itens **não** vira `[]` — propaga `DetailNotFound` para o orquestrador decidir (vira `sem_arquivo`/`sem_itens`, gravado como tal, não como erro).

### 4.3 Testes (`tests/test_client_detail.py`)

Usar `httpx.MockTransport` (sem rede):

- [ ] 200 lista → lista; 200 objeto → `[objeto]` em resultados.
- [ ] 204 → `[]`.
- [ ] 404 em resultados → `[]`; 404 em itens → `DetailNotFound`.
- [ ] 429 duas vezes e depois 200 → sucesso, com 2 backoffs (monkeypatch em `time.sleep`).
- [ ] 429 × `max_retries` → `DetailFetchError(status_code=429)`.
- [ ] 400 → `DetailFetchError` **sem** retry.

---

## 5. Normalização dos payloads de detalhe

Arquivo: [schemas.py](../src/pncp_collector/schemas.py). Reaproveitar `normalize(raw, model)`:
ele já faz snake_case + coerção por tipo de coluna.

- [ ] `normalize_item(raw, compra_key) -> dict` — `normalize(raw, Item)` + injeta `compra_key`.
- [ ] `normalize_resultado(raw, compra_key, numero_item, posicao) -> dict` — idem; se `sequencialResultado` faltar, usa `posicao` (decisão da 2.4).
- [ ] `normalize_arquivo(raw, documento_tipo, documento_id) -> dict` + `is_target_type` calculado por `filters.is_target_file_type` (atividade 6.1).
- [ ] Coerção de `Numeric` já cobre `valorUnitarioHomologado` como string/float. Verificar `quantidade` com vírgula decimal (se a 1.3 mostrar) — `parse_decimal` hoje não trata `"1,5"`.
- [ ] Testes em `tests/test_schemas_detail.py` com os payloads reais de `docs/API_DETALHE.md` (copiar e reduzir).

---

## 6. Regras de negócio da fase 2

Arquivo: [filters.py](../src/pncp_collector/filters.py) (funções puras) — ou novo
`detail_rules.py` se `filters.py` ficar grande demais. Sem rede, sem banco.

### 6.1 Tipo de arquivo alvo

- [ ] Constante `TARGET_FILE_TYPES = {"ata": {"Ata de Registro de Preço", ...}, "contrato": {"Contrato", ...}}` com os valores **exatos** confirmados na 1.2 (incluir variantes com/sem acento se existirem).
- [ ] `is_target_file_type(documento_tipo, tipo_documento_nome) -> bool`. Comparação exata após `strip()`; **não** fazer `lower()`/unaccent salvo se a 1.2 mostrar variação — e nesse caso documentar quais.
- [ ] Decidir (com base na 1.2) se `status_ativo = false` conta. Default: **conta** (o outro pipeline conta todos), com nota.
- [ ] `has_target_file(arquivos: list[dict], documento_tipo) -> bool`.

### 6.2 Homologação

- [ ] Constante `HOMOLOGADO = "Homologado"`.
- [ ] `homologated_items(itens: list[dict]) -> list[dict]`.
- [ ] Status da compra: `sem_itens` se lista vazia; senão `ok`. Status do **documento** `sem_homologado` se `homologated_items` vazio.

### 6.3 Vencedor e preço

Mesma regra do outro pipeline, § 3 "Preço":

- [ ] `pick_winner(resultados: list[dict]) -> dict | None`: entre os com `valor_unitario_homologado` válido (`not None`, `> 0`), o de menor `ordem_classificacao_srp` (`None` trata como infinito; empate → menor `sequencial_resultado`/posição).
- [ ] `is_winner` marcado só nele; os demais ficam gravados com `is_winner = false`.
- [ ] **Não** calcular "preço efetivo" (vencedor ou estimado) em coluna: fica para a consulta. Documentar a query de referência (§ 10.2).

### 6.4 Máquina de estados do documento

Função pura `decide_document_status(...)` que recebe o que foi obtido e devolve
`(status, error)`; a orquestração só executa. Ordem, igual ao outro pipeline:

```
sem_identificacao  ← parse_*_key falhou, ou contrato sem compra mesmo após fallback
sem_arquivo        ← has_target_file() falso (inclui 404 na lista de arquivos)
sem_homologado     ← compra sem item Homologado (inclui compra sem_itens)
erro               ← DetailFetchError em qualquer chamada (exceto as absorvidas acima)
ok                 ← chegou ao fim com ≥1 item Homologado
```

- [ ] Documentar a **política de revisita** por status (usada na 7.1):

| Status | Revisitado por padrão? | Por quê |
|---|---|---|
| `pendente` | sim | ainda não foi |
| `erro` | sim (até `PNCP_DETAIL_MAX_ATTEMPTS`) | transitório |
| `sem_homologado` | sim | homologação pode ter saído depois |
| `sem_arquivo` | **não** (`--revisit-sem-arquivo` liga) | raro mudar; mas um contrato recém-publicado pode ganhar o PDF dias depois — medir na 10.3 |
| `sem_identificacao` | não | dado da capa não muda sem nova fase 1 |
| `ok` | não (`--refresh` liga) | já tem tudo |

### 6.5 Testes (`tests/test_detail_rules.py`)

- [ ] Arquivo alvo: presente / ausente / só edital / tipo alvo inativo / lista vazia.
- [ ] Homologados: mistura de situações → só os `Homologado`.
- [ ] Vencedor: um resultado; três com ordens 3,1,2 → ordem 1; ordem `None` e valor válido vs ordem 1 e valor 0 → ordem `None`; todos com valor 0 → `None`.
- [ ] Status: uma tabela de casos (`pytest.mark.parametrize`) cobrindo cada linha da máquina de estados, inclusive a precedência (sem_arquivo **antes** de sem_homologado).

---

## 7. Orquestração da fase 2

Novo módulo `src/pncp_collector/detail.py` com `Detailer` (espelho do `Collector`). A fase 2
tem **quatro passos sequenciais**, cada um com seu próprio loop, progresso `rich` e commit em lote —
não um "por documento faz tudo". Motivo: o cache por compra (D3) funciona melhor quando o passo
de itens roda sobre o conjunto de compras distintas, e cada passo pode ser retomado sozinho.

```
passo A  resolver compra e listar arquivos   (por documento)
passo B  itens                                (por compra)
passo C  resultados                           (por item homologado)
passo D  fechar status dos documentos         (só banco)
```

### 7.1 Seleção do trabalho (queries em `db.py`)

- [ ] `select_documents_to_detail(session, tipo, statuses, limit, cnpj=None) -> Iterator[Ata | Contrato]` — em lotes (`yield_per`), ordenado por `data_assinatura desc` (o mais recente primeiro: se a execução for interrompida, o que ficou detalhado é o mais útil).
- [ ] `select_compras_to_fetch_items(session, statuses)` — só compras que têm **pelo menos um documento com arquivo alvo** (não gastar chamadas em compra cujos documentos já morreram em `sem_arquivo`).
- [ ] `select_items_to_fetch_results(session)` — itens `Homologado` de compras com `items_status = ok` e `results_status != ok`, sem linha em `itens_resultados` (ou todos, com `--refresh`).
- [ ] Respeitar `PNCP_CNPJ` como recorte também aqui (mesmo filtro que a fase 1 usa).

### 7.2 Passo A — por documento

Para cada documento selecionado (política da 6.4):

1. `parse_*_key`. Falhou → `sem_identificacao`, grava, próximo.
2. Contrato com `numero_controle_pncp_compra` vazio → `get_contrato` (fallback), extrai `numeroControlePncpCompra`, marca `compra_resolved_by = fallback`. Falhou → `sem_identificacao` (se `DetailNotFound`) ou `erro`.
3. `INSERT ... ON CONFLICT DO NOTHING` em `compras` com `items_status = pendente`; grava `compra_key` no documento.
4. `get_arquivos_*` → upsert em `arquivos`. Não apagar os antigos antes: upsert por PK; arquivo removido do PNCP fica com `updated_at` antigo — documentar.
5. `has_target_file` falso → `sem_arquivo`. Verdadeiro → documento continua `pendente` (o status final é do passo D); em ambos os casos `detail_attempts += 1`.
6. `DetailFetchError` → `erro` + `detail_error`, sem derrubar o loop.
7. Commit a cada `BATCH_SIZE` documentos (reusar a constante do `pipeline`).

### 7.3 Passo B — por compra

Para cada compra `pendente`/`erro` (e `ok` se `--refresh`):

1. `get_itens` → `normalize_item` → upsert em `itens`.
2. `total_itens`, `total_homologados`, `items_status = ok | sem_itens`, `items_fetched_at`.
3. `results_status = pendente` se houver homologados, senão `ok` (nada a buscar).
4. Erro → `items_status = erro`, `items_error`.
5. Commit por lote de compras.

### 7.4 Passo C — por item homologado

O passo mais caro (uma chamada por item). É aqui que concurrency importa.

1. Para cada compra com `results_status in (pendente, parcial, erro)`, para cada item selecionado na 7.1: `get_resultados` → `normalize_resultado` → `pick_winner` → upsert em `itens_resultados`.
2. Commit **por compra** (não por item): se cair no meio, a compra fica `parcial` e a próxima execução refaz só os itens sem resultado.
3. Ao terminar a compra sem erro → `results_status = ok`; com erro em algum item → `parcial` + contagem no log.
4. **Concurrency** (`PNCP_DETAIL_CONCURRENCY`, default 1, máx 8): `ThreadPoolExecutor` sobre os itens de **uma** compra por vez. `httpx.Client` é thread-safe, mas o `time.sleep(request_delay)` vira por-thread — com concurrency N o ritmo efetivo é N/delay. Começar com 1; só subir depois da medição da 1.6/10.3.

### 7.5 Passo D — fechar status

Só SQL, sem rede. Para cada documento em `pendente` com `compra_key` e com arquivo alvo:

- compra `items_status = sem_itens` ou `total_homologados = 0` → `sem_homologado`;
- compra `items_status = erro` → `erro` (herda `items_error`);
- compra `ok` com `total_homologados > 0` e `results_status = ok` → `ok`;
- `results_status = parcial` → continua `pendente` (a próxima rodada completa).

Um único `UPDATE ... FROM compras` por tipo de documento resolve; escrever em `db.py` como `close_document_statuses(session)`.

### 7.6 Registro da execução

- [ ] Reaproveitar `CollectionRun` com `dataset = "detalhe"` e `stats` contendo: documentos por status (antes/depois), compras consultadas, itens gravados, resultados gravados, chamadas por endpoint, erros por endpoint, duração por passo.
- [ ] `DetailStats` dataclass (espelho de `FilterStats`) com `as_dict()`.
- [ ] `render_detail_summary(console, stats)` em tabela `rich`, igual ao `render_summary`.

### 7.7 Testes (`tests/test_detail.py`)

Mesmo padrão de `test_pipeline.py`: `Detailer` recebe um cliente falso (dict de respostas por
URL) e a persistência substituída do mesmo jeito que `test_pipeline.py` faz hoje (`JSONB` e
`insert ... on conflict` são do dialeto Postgres; não rodam em SQLite).

- [ ] 2 atas + 1 contrato da mesma compra → `get_itens` chamado **uma** vez, `get_resultados` uma vez por item homologado.
- [ ] Contrato sem compra na capa → fallback chamado; com fallback 404 → `sem_identificacao`.
- [ ] Ata sem arquivo alvo → `sem_arquivo`, e `get_itens` **não** é chamado para uma compra que só tem documentos `sem_arquivo`.
- [ ] Compra sem homologado → documentos `sem_homologado`; segunda execução revisita e, com o fake agora devolvendo `Homologado`, vira `ok`.
- [ ] Erro em `get_resultados` no item 2 de 3 → compra `parcial`, documento `pendente`; segunda execução chama só o item 2.
- [ ] `sem_arquivo` não é revisitado sem a flag; `ok` não é revisitado sem `--refresh`.
- [ ] `detail_attempts` chega a `PNCP_DETAIL_MAX_ATTEMPTS` → documento sai da seleção padrão.

---

## 8. CLI

Arquivo: [cli.py](../src/pncp_collector/cli.py).

- [ ] `pncp-collector detalhar [--tipo ata|contrato|all] [--status pendente,erro,...] [--limit N] [--refresh] [--revisit-sem-arquivo] [--only-step A|B|C|D] [-v]`.
- [ ] `pncp-collector all` passa a rodar `atas → contratos → detalhar` (a ordem já importava; continua importando). Flag `--skip-detail` para o comportamento antigo.
- [ ] `pncp-collector status`: tabela com contagem de documentos por `detail_status` (por tipo) e de compras por `items_status`/`results_status`. É o painel de "quanto falta".
- [ ] `init-db` e `reset-db` não mudam (o `create_all` cobre as tabelas novas).

---

## 9. Configuração

Arquivo: [config.py](../src/pncp_collector/config.py) e [.env.example](../.env.example).

| Variável | Default | Uso |
|---|---|---|
| `PNCP_DETAIL_CONCURRENCY` | `1` | passo C (7.4) |
| `PNCP_DETAIL_REQUEST_DELAY` | `= REQUEST_DELAY` | só se a 1.6 mostrar limite diferente |
| `PNCP_DETAIL_MAX_ATTEMPTS` | `5` | documento/compra em `erro` com `detail_attempts >= N` deixa de ser revisitado automaticamente (evita ficar preso num 500 permanente) |
| `PNCP_TARGET_FILE_TYPES_ATA` / `_CONTRATO` | valores da 6.1 | override sem mexer em código, se o PNCP renomear |

---

## 10. Documentação e validação

### 10.1 README

- [ ] Nova seção "Detalhamento (fase 2)": os quatro passos, a máquina de estados, a política de revisita, o custo estimado em chamadas.
- [ ] § Banco: as 4 tabelas novas e as colunas de estado, mantendo a convenção de nomes.
- [ ] § Uso: `detalhar`, `status`, `all --skip-detail`.
- [ ] § Custo estimado: acrescentar a estimativa da 1.6 com os números reais.

### 10.2 Queries de referência (`docs/CONSULTAS.md`, novo)

- [ ] "Preço de referência por item": vencedor (`is_winner`) com fallback para `valor_unitario_estimado` quando não há resultado — a regra que o outro pipeline aplicava na coleta, aqui aplicada na leitura.
- [ ] "Itens de um documento": `atas/contratos → compra_key → itens (Homologado)`.
- [ ] "Documentos que nunca fecharam": `detail_status in (erro, pendente)` com `detail_attempts`.
- [ ] "Compras com mais de um vencedor por item" (o que a D6 passou a permitir medir).

### 10.3 Validação com dados reais

- [ ] Rodar `detalhar --limit 200` com `PNCP_CNPJ` de um órgão conhecido; conferir manualmente 5 documentos `ok` contra a página pública (`compras.url_pncp`): itens, quantidades, fornecedor e valor do vencedor batem.
- [ ] Conferir 5 `sem_arquivo` e 5 `sem_homologado`: o motivo se sustenta na página pública.
- [ ] Interromper (`Ctrl+C`) no meio do passo C e rodar de novo: nada é refeito além do que faltava (contar chamadas com `-v`).
- [ ] Registrar em `docs/API_DETALHE.md`: chamadas totais, duração, taxa de 429, distribuição final de status. Esse é o número que diz se a varredura nacional da fase 2 é viável — a fase 1 de atas já tem problema de escala conhecido, e a fase 2 não pode piorar isso.

---

## 11. Ordem de execução e dependências

```
1 (API real) ──► 2 (schema) ──► 3 (ids) ──► 4 (client) ──► 5 (schemas)
                                   │                            │
                                   └────────► 6 (regras) ◄──────┘
                                                  │
                                                  ▼
                                            7 (orquestração) ──► 8 (CLI) ──► 9 (config)
                                                                                │
                                                                                ▼
                                                                          10 (docs + validação)
```

3, 4, 5 e 6 são independentes entre si depois de 1 e 2, e todos têm testes sem rede. 7 é o
único que junta tudo. Sugestão de commits: um por atividade numerada, com 1 e 2 juntos.

---

## 12. Fora de escopo (explicitamente)

- Download de PDF (D5).
- Busca textual, termos, classificação, corte (etapas 1, 3 e 4 do outro pipeline).
- Decidir em qual ata um item "aparece de fato" (etapa 5 do outro pipeline).
- Resolver a escala da fase 1 de atas — problema separado, já conhecido.
- Alembic/migrações versionadas — enquanto for `create_all` + SQL manual, fica assim.

## 13. Perguntas em aberto (responder antes ou durante a atividade 1)

1. Item cancelado/deserto: gravar (D7) ou não? O plano diz gravar; custo é só disco.
2. `status_ativo = false` em arquivos conta como arquivo alvo? (6.1)
3. Contrato derivado de ata: detalhar sempre (D10) ou pular quando `PNCP_DISCARD_DERIVED_CONTRACTS` estiver ligado? Proposta: pular quando a flag estiver ligada — o contrato nem estaria na base.
4. Existe endpoint de resultados por compra? (1.4) Se sim, o passo C muda de N chamadas para 1 e a concurrency vira desnecessária.
