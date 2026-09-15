# API de detalhe do PNCP: o que foi verificado

Resultado da atividade 1 do [PLANO_DETALHAMENTO.md](PLANO_DETALHAMENTO.md). Tudo medido em
**2026-09-15** contra a base atual (500 atas e 391 contratos, todos do CNPJ `07954480000179`,
Estado do Ceará). Amostra: 36 atas, 21 contratos, 6 compras, 26 itens, 21 resultados.

Base dos endpoints: `https://pncp.gov.br/api/pncp/v1` (o servidor redireciona internamente para
`/pncp-api/v1`, que é o que aparece nos campos `url`/`path` das respostas).

---

## 1.1 Números de controle

Confirmado em 100 % da base (500 atas, 391 contratos):

| Campo | Formato | Exemplo |
|---|---|---|
| `numeroControlePNCPCompra` (ata e contrato) | `{cnpj:14}-1-{seq:6}/{ano:4}` | `07954480000179-1-025970/2025` |
| `numeroControlePNCPAta` | `{compra}-{seqAta:6}` | `07954480000179-1-025970/2025-000001` |
| `numeroControlePNCP` (contrato) | `{cnpj:14}-2-{seqContrato:6}/{ano:4}` | `07954480000179-2-031999/2026` |

- Atas: `numeroControlePNCPAta` sempre começa com `numeroControlePNCPCompra + "-"` (0 exceções).
- Contratos: **0 de 391** com `numero_controle_pncp_compra` vazio. O fallback D4 fica
  implementado mas, nesta base, não é exercitado.
- Atas: 0 de 500 sem compra.
- Nenhum formato divergente (sem zeros à esquerda, espaços, etc.). O parser da atividade 3 pode
  ser estrito: regex `^\d{14}-1-\d{6}/\d{4}$` e derivados.
- O `seq` do contrato é **sempre diferente** do `seq` da compra (391/391) — nunca usar um pelo outro.
- Compras distintas na base: 498 (atas) + 308 (contratos) = **752** na união.

## 1.2 Arquivos

`GET /orgaos/{cnpj}/compras/{ano}/{seq}/atas/{seqAta}/arquivos` e
`GET /orgaos/{cnpj}/contratos/{ano}/{seqContrato}/arquivos`.

- Resposta: **lista direta** (sem envelope), 200.
- Sem arquivo: **204 sem corpo** (1 ata em 36).
- Documento inexistente: **404** com `{"status":"404","error":"404 NOT_FOUND","message":"Ata não cadastrada."}`
  (contrato: `"Contrato não cadastrado. {cnpj} {ano} {seq}"`).
- **Não existe `statusAtivo`** em nenhum dos dois. A pergunta 13.2 do plano fica respondida:
  não há como distinguir inativo; tudo que vem conta.
- Os campos diferem entre ata e contrato:

| Campo | Ata | Contrato |
|---|---|---|
| `sequencialDocumento` | sim | sim |
| `titulo` | sim | sim |
| `tipoDocumentoNome` | sim | sim |
| `tipoDocumentoId` | sim (int) | **não** |
| `url` | sim | sim |
| `uri` | **não** | sim (= `url`) |
| `dataPublicacaoPncp` | sim (ISO com hora) | sim |
| `cnpj`, `anoCompra`, `sequencialCompra` | não | sim — **mas são o ano e o sequencial do contrato**, não da compra (nome enganoso) |

### Valores de `tipoDocumentoNome` vistos

| Documento | Valor | Ocorrências |
|---|---|---|
| ata | `Outros Documentos` (id 16) | **129 de 129** |
| contrato | `Contrato` | 9 |
| contrato | `Nota de Empenho` | 13 |
| contrato | `Outros Documentos` | 20 |

**Ata**: em 36 atas, nenhum arquivo do tipo "Ata de Registro de Preço". Os títulos mostram que a
ata está lá (`ATA DIGITALIZADA.pdf` ×12, `ATA.pdf` ×3, `ATA DE REGISTRO DE PRECO ASSINADA` ×2), ao
lado de `TERMO DE HOMOLOGACAO.pdf` ×31, extratos do DOE e aditamentos — tudo classificado como
`Outros Documentos`. Ou seja, **a classificação por tipo é inútil para atas neste órgão**.

**Contrato**: `tipo_contrato_nome` na base é `Contrato (termo inicial)` ×208 e `Empenho` ×183.
Os 8 contratos amostrados cujo único arquivo é `Nota de Empenho` são todos `Empenho`. A nota de
empenho é o instrumento do contrato-por-empenho; o preço vale do mesmo jeito.

### Decisão para a regra 6.1 (substitui o que o plano supunha)

| Documento | Tipos que contam como "arquivo alvo" | Override |
|---|---|---|
| ata | **qualquer arquivo** (conjunto vazio = todos) | `PNCP_TARGET_FILE_TYPES_ATA` |
| contrato | `Contrato`, `Nota de Empenho` | `PNCP_TARGET_FILE_TYPES_CONTRATO` |

Comparação exata após `strip()`; nenhuma variação de acento/caixa foi vista. Se um órgão
classificar as atas corretamente, `PNCP_TARGET_FILE_TYPES_ATA="Ata de Registro de Preço"`
restringe sem mexer em código.

## 1.3 Itens

`GET /orgaos/{cnpj}/compras/{ano}/{seq}/itens`

- **Paginado**, e o default é traiçoeiro: sem parâmetros vem só a **primeira página de 10**
  (compras com 14 e 21 itens devolveram 10). Parâmetros: `pagina` (1-based) e `tamanhoPagina`.
- `tamanhoPagina=50`, `500` e `1000` foram aceitos (200) e devolveram os 21 itens inteiros. Não
  há compra grande o bastante na amostra para achar o teto; o cliente usa **500** e pagina até
  vir página vazia.
- Página além do fim: **200 com `[]`** (não 204, não 404).
- Compra inexistente: **404** `"Contratação não cadastrada."`.
- `GET .../itens/quantidade`: devolve **inteiro puro** (`4`, `14`, `21`). Serve para saber quantas
  páginas pedir, mas como a paginação termina em `[]` ele é dispensável — **não usar** (economiza
  1 chamada por compra).
- Sem envelope: lista direta.
- Acentuação chega correta (UTF-8); `Menor preço` é `U+00E7`.

Campos (nomes exatos; **atenção**: é `situacaoCompraItem`, não `situacaoCompraItemId`):

```
numeroItem, descricao, materialOuServico, materialOuServicoNome, valorUnitarioEstimado,
valorTotal, quantidade, unidadeMedida, orcamentoSigiloso, itemCategoriaId, itemCategoriaNome,
patrimonio, codigoRegistroImobiliario, criterioJulgamentoId, criterioJulgamentoNome,
situacaoCompraItem, situacaoCompraItemNome, tipoBeneficio, tipoBeneficioNome,
incentivoProdutivoBasico, dataInclusao, dataAtualizacao, temResultado, imagem,
aplicabilidadeMargemPreferenciaNormal, aplicabilidadeMargemPreferenciaAdicional,
percentualMargemPreferenciaNormal, percentualMargemPreferenciaAdicional, ncmNbsCodigo,
ncmNbsDescricao, catalogo, categoriaItemCatalogo, catalogoCodigoItem, informacaoComplementar,
tipoMargemPreferencia, exigenciaConteudoNacional
```

Valores numéricos vêm como **float JSON** (`0.0951`, `4638000.0`), nunca string com vírgula —
`parse_decimal` atual serve.

`situacaoCompraItemNome` visto: `Homologado` (id 2) ×26, `Fracassado` ×3. A lista completa
(Cancelado, Deserto, Em Andamento, ...) não apareceu na amostra; a regra 6.2 compara só com
`Homologado`, então isso não bloqueia.

**`temResultado` importa**: 3 itens `Homologado` com `temResultado = false` responderam 204 em
`/resultados`. O passo C deve chamar resultados **só para `temResultado = true`** — item
homologado sem resultado é um caso real e a chamada é desperdício.

## 1.4 Resultados

`GET /orgaos/{cnpj}/compras/{ano}/{seq}/itens/{numeroItem}/resultados`

- Resposta: **lista direta** de resultados (21/21 casos com exatamente 1 elemento).
- Item sem resultado (`temResultado = false`): **204 sem corpo**. Não vimos 404 para item
  existente.
- `sequencialResultado` existe (int, `1` em todos) — é a PK que a 2.4 previa.
- `ordemClassificacaoSrp` veio `1` em todos (compras SRP com um único resultado). Não houve caso
  de mais de um resultado por item; a regra de vencedor (6.3) fica como planejada, sem
  evidência contrária.
- Endpoint agregado por compra: `GET .../compras/{ano}/{seq}/resultados` → **404** (`No message
  available`). **Não existe.** O passo C é mesmo 1 chamada por item com resultado.

Campos:

```
sequencialResultado, numeroItem, numeroControlePNCPCompra, niFornecedor,
nomeRazaoSocialFornecedor, tipoPessoa, codigoPais, porteFornecedorId, porteFornecedorNome,
naturezaJuridicaId, naturezaJuridicaNome, localidadeFornecedor, quantidadeHomologada,
valorUnitarioHomologado, valorTotalHomologado, percentualDesconto, ordemClassificacaoSrp,
dataResultado, dataInclusao, dataAtualizacao, dataCancelamento, motivoCancelamento,
situacaoCompraItemResultadoId, situacaoCompraItemResultadoNome, indicadorSubcontratacao,
aplicacaoMargemPreferencia, aplicacaoBeneficioMeEpp, aplicacaoCriterioDesempate,
amparoLegalMargemPreferencia, amparoLegalCriterioDesempate, paisOrigemProdutoServico,
localidadeExterior, moedaEstrangeira, valorNominalMoedaEstrangeira,
dataCotacaoMoedaEstrangeira, timezoneCotacaoMoedaEstrangeira, reservaRemanescente{codigo,nome}
```

`situacaoCompraItemResultadoNome` visto: `Informado` (id 1). `reservaRemanescente` é objeto
aninhado (vira `reserva_remanescente_codigo`/`_nome` no achatamento).

## 1.5 Fallback do contrato

`GET /orgaos/{cnpj}/contratos/{ano}/{seqContrato}` → 200 com o **mesmo payload da consulta** mais
`orgaoParteEnvolvida`, `tipoParteEnvolvida`, `unidadeOrgaoParteEnvolvida` e `tipoContrato.descricao`
/`statusAtivo`. Traz `numeroControlePncpCompra` e `numeroControlePncpAta` (6/6). Inexistente: 404
`"O contrato/empenho informado não está cadastrado."`.

## 1.6 Ritmo

Com delay de 0,5 s, 114 chamadas sequenciais, todas em `/api/pncp/v1/*`:

| Métrica | Valor |
|---|---|
| Latência média | **8,1–9,0 s** |
| p95 | 14,7–16,5 s |
| Máximo | 15,6 s |
| 429 | **0** |
| 5xx | 0 |

A API de detalhe é **lenta, não limitada**: nenhum 429 em ~15 min, mas cada chamada leva quase
10 s. É o oposto da API de consulta (rápida, mas com 429 frequente). Isso muda a conta:

```
chamadas ≈ docs (891) × 1 arquivos                         =   891
         + contratos_sem_compra (0) × 1 fallback           =     0
         + compras_distintas (752) × 1 itens (1 página)    =   752
         + itens_com_resultado (~752 × 7,3 itens × 0,88)   ≈ 4.800
                                                           ≈ 6.450 chamadas
```

(7,3 itens/compra e 88 % com `temResultado` são a média da amostra de 6 compras — chute grosso.)

A **8,5 s por chamada, sequencial: ~15 h**. Com 8 threads e a latência sendo do servidor (não
throttling), ~2 h. **`PNCP_DETAIL_CONCURRENCY` deixa de ser opcional** — e vale para os passos A
e B também, não só o C como o plano previa. Falta medir se a latência cai fora do horário
comercial e se a concorrência provoca 429 (atividade 10.3).

`PNCP_DETAIL_REQUEST_DELAY` não precisa ser diferente de `REQUEST_DELAY` por enquanto (0 × 429).

## Respostas às perguntas em aberto (§ 13 do plano)

1. Item cancelado/deserto/fracassado: **gravar** (D7 mantida). Vimos `Fracassado`; custa só disco.
2. `statusAtivo` em arquivos: **o campo não existe**. Tudo que a lista devolve conta.
3. Contrato derivado de ata: detalhar sempre (D10). A flag `PNCP_DISCARD_DERIVED_CONTRACTS` já o
   tira da base na fase 1; se ele está na base, é detalhado.
4. Endpoint de resultados por compra: **não existe** (404). Concurrency é obrigatória.

---

## Payloads de exemplo (reais, reduzidos)

### arquivos de ata
```json
[
  {
    "url": "https://pncp.gov.br/pncp-api/v1/orgaos/07954480000179/compras/2025/25970/atas/1/arquivos/1",
    "tipoDocumentoNome": "Outros Documentos",
    "dataPublicacaoPncp": "2026-04-30T13:50:39",
    "sequencialDocumento": 1,
    "titulo": "TERMO DE HOMOLOGACAO.pdf",
    "tipoDocumentoId": 16
  },
  {
    "url": "https://pncp.gov.br/pncp-api/v1/orgaos/07954480000179/compras/2025/25970/atas/1/arquivos/2",
    "tipoDocumentoNome": "Outros Documentos",
    "dataPublicacaoPncp": "2026-06-17T10:01:43",
    "sequencialDocumento": 2,
    "titulo": "EXTRATO TERMO DE HOMOLOGACAO.pdf",
    "tipoDocumentoId": 16
  }
]
```
### arquivos de contrato
```json
[
  {
    "uri": "https://pncp.gov.br/pncp-api/v1/orgaos/07954480000179/contratos/2026/31999/arquivos/1",
    "url": "https://pncp.gov.br/pncp-api/v1/orgaos/07954480000179/contratos/2026/31999/arquivos/1",
    "dataPublicacaoPncp": "2026-09-01T16:10:28",
    "cnpj": "07954480000179",
    "anoCompra": 2026,
    "sequencialCompra": 31999,
    "sequencialDocumento": 1,
    "titulo": "20260901.1454621.Integra.CONTRATO",
    "tipoDocumentoNome": "Contrato"
  }
]
```
### item
```json
{
  "numeroItem": 1,
  "descricao": "DIETA, NORMOCALORICA, ENRIQUECIDA COM TAURINA, CARNITINA, ISENTA DE SACAROSE E GLUTEN, OSMOLARIDADE MENOR OU IGUAL A 320 MOSM/L, ENTERAL SISTEMA FECHADO, PARA CRIANCAS ATE 10 ANOS, UNIDADE 1.0 MILILITRO",
  "materialOuServico": "M",
  "materialOuServicoNome": "Material",
  "valorUnitarioEstimado": 0.0951,
  "valorTotal": 441073.8,
  "quantidade": 4638000.0,
  "unidadeMedida": "UNIDADE 1.0 MILILITRO ",
  "orcamentoSigiloso": false,
  "itemCategoriaId": 3,
  "itemCategoriaNome": "Não se aplica",
  "patrimonio": null,
  "codigoRegistroImobiliario": null,
  "criterioJulgamentoId": 1,
  "criterioJulgamentoNome": "Menor preço",
  "situacaoCompraItem": 2,
  "situacaoCompraItemNome": "Homologado",
  "tipoBeneficio": 4,
  "tipoBeneficioNome": "Sem benefício",
  "incentivoProdutivoBasico": false,
  "dataInclusao": "2026-02-26T09:40:40",
  "dataAtualizacao": "2026-04-24T09:25:35",
  "temResultado": true,
  "imagem": 0,
  "aplicabilidadeMargemPreferenciaNormal": false,
  "aplicabilidadeMargemPreferenciaAdicional": false,
  "percentualMargemPreferenciaNormal": null,
  "percentualMargemPreferenciaAdicional": null,
  "ncmNbsCodigo": null,
  "ncmNbsDescricao": null,
  "catalogo": null,
  "categoriaItemCatalogo": null,
  "catalogoCodigoItem": null,
  "informacaoComplementar": null,
  "tipoMargemPreferencia": null,
  "exigenciaConteudoNacional": false
}
```
### resultado
```json
[
  {
    "indicadorSubcontratacao": false,
    "reservaRemanescente": {
      "codigo": 1,
      "nome": "Não se aplica"
    },
    "timezoneCotacaoMoedaEstrangeira": null,
    "moedaEstrangeira": null,
    "dataInclusao": "2026-04-24T09:25:35",
    "numeroItem": 1,
    "niFornecedor": "49324221000104",
    "dataCancelamento": null,
    "dataAtualizacao": "2026-04-24T09:25:35",
    "tipoPessoa": "PJ",
    "nomeRazaoSocialFornecedor": "FRESENIUS KABI BRASIL LTDA.",
    "valorTotalHomologado": 252771.0,
    "valorNominalMoedaEstrangeira": null,
    "dataCotacaoMoedaEstrangeira": null,
    "codigoPais": "BRA",
    "porteFornecedorId": 3,
    "quantidadeHomologada": 4638000.0,
    "valorUnitarioHomologado": 0.0545,
    "percentualDesconto": 0.0,
    "amparoLegalMargemPreferencia": null,
    "amparoLegalCriterioDesempate": null,
    "paisOrigemProdutoServico": null,
    "localidadeExterior": null,
    "ordemClassificacaoSrp": 1,
    "dataResultado": "2026-04-24",
    "motivoCancelamento": null,
    "situacaoCompraItemResultadoId": 1,
    "porteFornecedorNome": "Demais",
    "situacaoCompraItemResultadoNome": "Informado",
    "sequencialResultado": 1,
    "naturezaJuridicaNome": null,
    "localidadeFornecedor": null,
    "aplicacaoMargemPreferencia": false,
    "aplicacaoBeneficioMeEpp": false,
    "aplicacaoCriterioDesempate": false,
    "naturezaJuridicaId": null,
    "numeroControlePNCPCompra": "07954480000179-1-025970/2025"
  }
]
```
### contrato detalhe (recorte)
```json
{
  "numeroControlePNCP": "07954480000179-2-031999/2026",
  "numeroControlePncpCompra": "07954480000179-1-026144/2025",
  "numeroControlePncpAta": null,
  "anoContrato": 2026,
  "sequencialContrato": 31999,
  "tipoContrato": {
    "id": 1,
    "nome": "Contrato (termo inicial)",
    "descricao": "Instrumento formal que estabelece o vínculo jurídico inicial entre a Administração Pública e o contratado, definindo direitos, obrigações, prazos e condições de execução.",
    "statusAtivo": true
  },
  "dataAssinatura": "2026-09-01",
  "numeroRetificacao": 0,
  "orgaoParteEnvolvida": null,
  "tipoParteEnvolvida": null,
  "unidadeOrgaoParteEnvolvida": null
}
```
