# pncp-collector: continuar o trabalho em outro PC

Prompt de passagem escrito em 2026-11-16 por Claude, na sessão do PC antigo, para a sessão
nova. Peça ao Claude para ler `docs/CONTINUAR_AQUI.md` na primeira mensagem. Quem fala com
você é Diego Winter; o projeto é dele. A memória da sessão antiga está em
`docs/claude-memory-2026-11-16/` (ver no fim como instalar).

## O que é o projeto, em uma frase

Montar uma pesquisa de preços de itens de segurança pública (PLASEG: armamento, munição,
proteção balística, fardamento operacional, viaturas, rádios e TI, drones, bicicletas de
patrulha, concursos/TAF, CFTV e controle de acesso) a partir das atas de registro de preços e
contratos do PNCP, que não têm categorização nenhuma: cada órgão descreve o item como quer.

## Como chegamos aqui (decisões já tomadas, não rediscutir)

1. **O lake.** O coletor (`pncp-collector`, Python, Postgres) baixa todas as capas de atas
   e contratos vigentes do último ano (fase 1) e, por compra, os itens (fase 2, passo B).
   Tudo fica no banco; análises reexecutam sem reconsultar o PNCP.
2. **Triagem de capa (fase 3) virou só priorizador.** O Jev (`typesafe/jev-1.14`, endpoint
   de decisões do OpenRouter) leu o objeto das 143.917 compras de ata e marcou
   potencial / indefinido / fora_por_ora. Medimos que 5,8% das `fora_por_ora` escondiam item
   de interesse (um item solto numa compra mista) e que metade das `potencial` não tinha
   nada. Conclusão: a capa não decide; o item decide. Nada marcado `fora_por_ora` é descartado.
3. **O passo B é barato, o passo C é caro.** Baixar itens é 1 chamada por compra, grátis,
   ~5 compras/s com `PNCP_DETAIL_CONCURRENCY=4` (8 funciona). Buscar o resultado homologado
   (passo C) é 1 chamada por item. Por isso: itens de TUDO, resultado só dos itens aprovados.
   Passos da fase 2: A = arquivos (PDFs) por documento; B = itens por compra; C = resultado
   homologado por item; D = fechar status dos documentos.
4. **O Jev é rotulador, não classificador (fase 4).** Rotulou 101.774 descrições distintas de
   item (US$ 11,96 no total) em 15 categorias (14 do pesquisa-de-precos-pncp + `outros`).
   Contra 300 descrições validadas por humano (docs/validacao): 269/274 iguais, recall de
   interesse 51/51. Tabela `item_labels`. Não há mais exemplo de classe rara para comprar:
   a regex da peneira já pegou todo "fuzil/bicicleta/drone/concurso" do lake.
5. **Classificador local (fase 5).** TF-IDF (descrição em palavras e caracteres + objeto da
   compra como contexto) + regressão logística, treinado em `item_labels`, roda de graça na
   CPU. Holdout: acerto 0,953, interesse P=0,83 R=0,98 (`class_weight=balanced`, favorece
   recall, é o filtro do passo C). Gabarito humano: acerto 0,96. Comandos `treinar-itens`
   e `prever-itens`. O binário fica em `models/` (fora do git): é derivado de dados + código.
6. **Convenções.** Colunas e variáveis nossas em inglês; campos do PNCP em snake_case do
   nome original; VALORES de classificação em português (`potencial`, `vestuario_operacional`).
   Grafia de serviços `servico_*`. Chave do OpenRouter só no `.env`. Commits:
   `<tipo>(<escopo>): <resumo imperativo minúsculo>`; Diego pediu SEM linha de coautoria.
   Diego prefere explicações simples e diretas; quando algo não fica claro ele pergunta, e a
   resposta certa é recapitular do zero, sem jargão.

## Estado do banco no dump (`D:\pncp-backup\pncp_railway_2026-11-16.dump`, 1,3 GB; 19 GB restaurado)

| Tabela | Estado |
|---|---|
| atas / contratos | capas coletadas; contratos cresceram para ~1,93 M linhas |
| compras | 724.342 (143.917 de ata, 580.425 só de contrato), semeadas; triagem de capa feita só nas de ata |
| itens | 7.491.628 linhas: 100% das compras de ata (143.908 ok, 9 sem_itens) e 213.500 das 580.425 de contrato |
| item_labels | 101.774 rótulos do Jev (fatias peneira 49.596, reserva 10.000, aleatorio 30.000, ativo 12.178) |
| itens.predicted_* | previsão gravada em 615.887 itens com o modelo `itens-20261116-145326`; o resto está sem previsão |
| itens_resultados / arquivos | vazias: passo C e passo A ainda não rodaram |

## Como colocar o PC novo para funcionar

```
git clone <repositório>                     # branch main
# copiar o .env do PC antigo para a raiz (PNCP_DATABASE_URL, PNCP_OPENROUTER_API_KEY,
# PNCP_DETAIL_CONCURRENCY=4, PNCP_TRIAGE_CONCURRENCY=16, PNCP_TRIAGE_MAX_COST_USD=12)
uv sync
createdb -p 5433 pncp                       # PC novo: PostgreSQL 16 na porta 5433
pg_restore -p 5433 -d pncp -j 8 D:\pncp-backup\pncp_railway_2026-11-16.dump
# copiar D:\pncp-backup\models para <repositório>\models  (senão prever-itens retreina com
# outro model_id e refaz os 615 mil já previstos; retreinar leva ~8 min e também serve)
uv run pytest                               # 171 testes, sem rede nem Postgres
uv run pncp-collector status
```

## O que rodar primeiro (retomam de onde pararam; podem rodar ao mesmo tempo)

```
uv run pncp-collector prever-itens                      # 6,9 M itens restantes, ~1 h
uv run python scripts/baixar_itens.py --origem contrato # 366.925 compras, ~20 h
```

Use `PYTHONUTF8=1` se o console reclamar de codificação (o spinner do rich usa braille).

## O que falta construir (próximo trabalho, nesta ordem)

1. **Passo C filtrado pelo classificador.** FEITO em 2026-11-17: `detalhar` busca resultado só
   dos itens com `predicted_interest >=` limiar (0,5, guardado na tabela `settings`; comando
   `limiar`), homologados e com
   resultado, sem depender do passo A; o passo A e o D ficam só nas capas dessas compras
   (`--todos-os-itens` desliga). Resultado válido = situação `Informado` e preço > 0
   (`is_valid`, 1:n por item; cancelado não vence). Falta rodar: ~96 mil itens em ~31 mil
   compras (`uv run pncp-collector detalhar --only-step C`).
2. **Precisão do classificador.** Erros conhecidos: "colete" não operacional vira
   vestuario_operacional; peça/locação de veículo vira viatura; descrição vazia
   ("conforme anexo", "LOTE Nº 46") é impossível. Caminhos: limiar por classe, ou
   `--class-weight none` para a categoria final (P=0,91) mantendo `balanced` só como filtro.
3. **Contratos derivados de ata** herdam a classificação da compra (40/40 na validação).
4. **Objetos indefinidos**: fila para OCR dos PDFs (passo A traz os links). A fazer.
5. `collection_runs.dataset='itens_treino'` é o nome antigo de `scripts/baixar_itens.py`,
   mantido de propósito para não quebrar o histórico.
6. **Tira-teima dos itens em dúvida**: só possibilidade futura (README, fase 5). Decisão de
   2026-11-18: o Jev serve só para gerar rótulo de treino; a operação roda no classificador
   local com a regra do limiar, para não depender de modelo fechado no dia a dia.

## Armadilhas já vistas

- Compra com 5.551 itens estourava os 65.535 parâmetros do Postgres: `upsert` agora grava em
  lotes; o script marca a compra como `erro` com mensagem curta se o banco recusar.
- `pg_dump` rodando dentro do Bash do Claude perdia permissão de escrita no disco externo
  no meio do arquivo; pelo PowerShell, como processo normal, funcionou.
- A API do PNCP não devolve CATMAT nos itens (0 em 1.459 testados); o código estruturado só
  existe na API do Compras.gov.br, que cobre ~1/3 das atas (origem "Contratos.gov.br").
  Ideia guardada, não implementada.
- Exército (CNPJ 00394452000103) entra como `potencial` na capa por "Regimento/Batalhão"
  mesmo comprando areia; isso não importa mais, porque o item decide.
- O Railway (banco remoto original) ficou sem disco em 2026-11-14; desde então tudo roda no
  Postgres local, hoje o banco `pncp` no PostgreSQL 16 (porta 5433; antes se chamava
  `pncp_railway`). A caixa (TV box de 1 GB) não serve para treino, só coleta.

## Arquivos de referência no repositório

- `README.md`: seções Triagem por objeto (fase 3), Rotulagem de itens (fase 4),
  Classificador local de itens (fase 5), Banco, Configuração.
- `docs/validacao/README.md`: gabaritos humanos (200 compras de capa; 300 descrições de item).
- `src/pncp_collector/item_rules.py`: as 15 categorias e a regex da peneira.
- `src/pncp_collector/item_classifier.py`: treino e previsão.
- `scripts/baixar_itens.py`: passo B para todas as compras.
- Migrações para banco já existente: `docs/MIGRACAO_*.sql` (o dump já tem tudo aplicado).

## Memória do Claude

A pasta `docs/claude-memory-2026-11-16/` é a cópia da memória persistente da sessão antiga
(decisões, preferências de Diego, pendências). Para o Claude Code do PC novo enxergar, copie
o conteúdo para `%USERPROFILE%\.claude\projects\<slug-do-caminho-do-projeto>\memory\`, onde o
slug é o caminho absoluto do repositório com `\` e `:` trocados por `-` (no PC antigo era
`c--Users-diego-Documents-dev-plaseg-pncp-collector`). Depois disso a pasta pode sair do repo.
