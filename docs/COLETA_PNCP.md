# Coleta no PNCP (etapa 2) — o que entra, o que sai, de onde vem

Resumo de referência da etapa 2. O código-fonte é a autoridade:
`steps/e2_collect.py` (orquestração) e `core/collection/` (busca, arquivos, itens, URLs).
Atualizado em 2026-09-15.

## 1. De onde vem

Tudo vem do PNCP, em quatro chamadas encadeadas por documento:

| # | O quê | Endpoint | Para quê |
|---|---|---|---|
| 1 | Busca textual | `GET https://pncp.gov.br/api/search/?q=<termo>&tipos_documento=contrato\|ata&status=vigente&ordenacao=-data` | Descobrir documentos. Uma busca por (termo da etapa 1 × fonte). Só `vigente`. Páginas de 500; teto de 10.000 resultados por busca (limite do Elasticsearch do PNCP — acima disso ficam só os mais recentes). |
| 2 | Lista de arquivos | contrato: `/api/pncp/v1/orgaos/{cnpj}/contratos/{ano}/{seq}/arquivos`<br>ata: `/api/pncp/v1/orgaos/{cnpj}/compras/{ano}/{seq}/atas/{seqAta}/arquivos` | Confirmar que existe arquivo do tipo certo. **Nada é baixado** (ADR-011): só se guardam os identificadores para a etapa 5 baixar depois do corte. |
| 3 | Itens da compra | `/api/pncp/v1/orgaos/{cnpj}/compras/{ano}/{seq}/itens` (+ `/itens/quantidade`) | A lista de itens. Sempre por **compra** — a API não tem itens por ata nem por contrato (ADR-024). |
| 4 | Resultado vencedor | `/api/pncp/v1/orgaos/{cnpj}/compras/{ano}/{seq}/itens/{n}/resultados` | Preço homologado e fornecedor de cada item. |

### Ata × contrato: a única diferença real

- **Ata**: a busca já devolve o sequencial da compra (`numero_sequencial_compra_ata`) e o
  sequencial da ata (`numero_sequencial`). Vai direto aos itens.
- **Contrato**: a busca devolve o sequencial do **contrato**, que não é o da compra. É preciso
  uma chamada extra a `/orgaos/{cnpj}/contratos/{ano}/{seq}` para ler
  `numeroControlePncpCompra` e extrair o sequencial da compra
  (`fetch_items.resolver_sequencial_compra_contrato`). Se isso falhar, o contrato vira
  `sem_homologado` (pendente).

Daí em diante o caminho é o mesmo.

## 2. Regras de entrada e descarte

Aplicadas por documento, nesta ordem (`collect_pncp._coletar_de_base`):

| Status | Condição | Destino |
|---|---|---|
| `sem_identificacao` | Falta CNPJ, ano ou sequencial; ou ata sem sequencial de ata. | Descartado. Marcado como visto para não reprocessar. |
| `sem_arquivo` | Nenhum arquivo com `tipoDocumentoNome` exatamente `"Contrato"` ou `"Ata de Registro de Preço"`. | Descartado. Marcado como visto. |
| `sem_homologado` | Nenhum item da compra com `situacaoCompraItemNome == "Homologado"` (ou contrato sem compra resolvida). | **Pendente** em `coleta_pendente`. Revisitado em toda rodada `atualizar`; se a homologação saiu, entra. |
| `erro` | Exceção na API (depois de esgotar retries). | `erro_item`. Não derruba a etapa. |
| `ok` | Passou por tudo e tem ≥1 item homologado. | Grava `documento` + 1 linha em `item` por item homologado, na **mesma transação**. |

Sobre os arquivos: **todos** os do tipo alvo contam (documento original, aditivos, apostilamentos,
prorrogações publicados com o mesmo `tipoDocumentoNome`). Qualquer outro tipo (edital, termo de
referência, etc.) é ignorado nesta checagem.

Sobre os itens: só `"Homologado"`. Cancelado, deserto, fracassado, em andamento e qualquer
outra situação ficam de fora.

### Dedup e parada

- **Dedup por documento** (`numeroControlePNCP`): documento já coletado numa busca anterior
  não é reconsultado — só ganha o vínculo com o termo novo em `documento_termo`. Documento
  visto e descartado é ignorado de vez.
- **Cache por compra** (ADR-024): as N atas de um mesmo pregão consultam itens e resultados
  **uma vez**. Antes disso o pregão 507 da Embrapa (25 atas × 88 itens) gerava 2.200+ chamadas.
- **Watermark** (só com `atualizar` marcado): a busca vem ordenada por
  `data_atualizacao_pncp` desc; ao cruzar a maior data já vista para aquele (termo, fonte), a
  paginação para. Progresso e watermark são gravados na mesma transação ao fim de cada busca.
- **Retry**: 429 e 5xx retentam com backoff (até 6×); 4xx é terminal (404/410 = recurso removido,
  400 = paginação além da janela). Página que esgota tentativas é pulada, não aborta.

## 3. O que fica gravado

### Identidade do item

```
item_key    = <compra_key>::<numeroItem>
compra_key  = 00348003000110-1-000507/2025          ← prefixo do número de controle até o ano
```

A chave é a **compra**, nunca a ata ou o contrato (ADR-024). É isso que impede N atas do
mesmo pregão de multiplicar os itens. `compra_key` sai só de `core.collection.urls.chave_compra`.
Para contrato, `compra_key` coincide com o próprio número de controle (não tem sufixo de ata) —
é a mesma regra, não um caso especial.

Em qual documento o item foi de fato encontrado é resultado da **etapa 5**, não desta.

### Preço

- `preco_unitario` = `valorUnitarioHomologado` do **vencedor**: entre os resultados com valor
  válido (≠ null/""/0), o de menor `ordemClassificacaoSrp`.
- Só cai no `valorUnitarioEstimado` do edital quando o item não tem resultado (`temResultado`
  falso ou lista vazia). O estimado costuma ser placeholder (0 / 0,01).
- `preco_estimado` é sempre preservado à parte, para comparação.

### Demais colunas (`COLUNAS_ITENS`)

| Grupo | Colunas |
|---|---|
| Item | `numeroItem`, `descricao_api`, `unidade`, `quantidade`, `fornecedor`, `data_resultado` |
| Documento | `tipo_doc`, `numeroControlePNCP`, `orgao`, `orgao_cnpj`, `uf`, `ano`, `data` (publicação, ou assinatura como fallback), `data_assinatura`, `data_fim_vigencia` |
| Rastreio | `conceitos_origem` (termo que achou o documento) |
| Para a etapa 5 | `numero_sequencial`, `numero_sequencial_ata`, `url_pncp` (URL pública reconstruída — rede de segurança do ADR-012 para rebaixar sob demanda) |

No banco isso vira `documento` (uma linha por documento) + `item` (uma por item) +
`documento_termo` (N:N documento × termo). Estado de execução: `coleta_progresso`,
`collection_watermark`, `coleta_pendente`.

## 4. O que NÃO acontece aqui

- Não baixa PDF — etapa 5, só para o que sobrevive ao corte da 4.
- Não classifica nem corta — etapas 3 e 4.
- Não decide em qual ata o item aparece — etapa 5.
- Não guarda mais de um vencedor por item: `fetch_resultado_vencedor` devolve um único
  resultado. Se um item tiver mais de um fornecedor homologado, o banco não sabe (pendência
  conhecida, ver CLAUDE.md § ADR-024).

## 5. Parâmetros da etapa (`Params`)

| Campo | Default | Efeito |
|---|---|---|
| `conceitos` | — | Restringe a estes termos (vírgula-separados). |
| `ignorar_cache` | `False` | Refaz buscas já concluídas e zera os pendentes em memória. |
| `atualizar` | `False` | Revisita todos os (termo, fonte), parando no watermark; revisita pendentes primeiro. |
| `limite_termos` | — | Máximo de termos (teste). |
| `tam_pagina` | 500 | Tamanho da página da busca. |
| `concurrency` | 3 | Documentos processados em paralelo dentro de cada busca (1–16). A paginação continua sequencial, porque o watermark depende da ordem. |
