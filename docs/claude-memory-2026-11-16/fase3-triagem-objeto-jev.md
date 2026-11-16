---
name: fase3-triagem-objeto-jev
description: Nova fase do pncp-collector (decidida 2026-11-13) - triagem por objeto da capa com o modelo typesafe/jev-1.14 (endpoint OpenRouter /api/alpha/decisions) para escolher quais compras passam pela fase 2
metadata:
  node_type: memory
  type: project
  originSessionId: 3cb4cb0f-fb70-4b8f-bd6a-48a4c8e872ba
  modified: 2026-11-13T20:23:34.650Z
---

Decisões de 2026-11-13 com Diego sobre a fase 3 (triagem por objeto):

- **Mora no pncp-collector**, não nos repos irmãos (pesquisa-de-precos-pncp / pipeline-csv-congelado). O collector é o lake; análises reexecutam sem reconsultar o PNCP.
- **Unidade = compra** (numero_controle_pncp_compra). 527k atas -> 144k compras; objeto é o mesmo em todas as atas de uma compra (1 exceção em 144k). Tabela `compras` existia vazia e sem `objeto`; o objeto vem da capa (ata.objeto_contratacao / contrato.objeto_contrato).
- **Domínio é segurança pública (PLASEG)**, não saúde. 14 categorias de item no pesquisa-de-precos-pncp; na capa usamos 6 grupos (valores em português, ver [[convencao-nomes-controle-ingles]]): armamento, vestuario_operacional, viaturas, ti_comunicacao, drone_bicicleta, servicos.
- **Estados**: potencial / fora_por_ora / indefinido (valores em português; nomes de coluna em inglês, ex. triage_status). Nada é descartado. `indefinido` (objeto genérico ou "ver edital") fica como to-do: precisa de OCR dos PDFs (arquivos vêm na fase 2) para classificar.
- **Contratos derivados de ata** herdam a classificação da compra (reaproveitamento), desde que validado; em 40 pares ata x contrato com objetos diferentes, o Jev deu a mesma decisão em 40/40.
- **Modelo**: typesafe/jev-1.14 só funciona em POST https://openrouter.ai/api/alpha/decisions (perguntas tipadas choice/noul/score, retorna probabilidades). `typesafe/jev-router` é chat e cai em modelo caro. Medido na implementacao final (criterios longos): ~US$0,000076/compra, ~1.800 tokens de entrada; 326k compras semeadas (144k de atas + 182k so de contratos) -> lote completo ~US$25, so vigentes de atas ~US$10. API é alfa: isolar numa camada pequena.
- **Validação**: Diego rotula pessoalmente docs/validacao/amostra_triagem_2026-11-13.xlsx (200 compras: 100 aleatórias, 60 com palavra-chave, 40 genéricas) + 40 pares. Previsões do Jev guardadas à parte em *_jev.csv para medir recall de "potencial" sem enviesar.
- Grafia de categorias de serviço: usar `servico_*` (o pesquisa-de-precos-pncp tem rename inconsistente para `service_*`).

**Why:** o lake tem 527k atas, maioria saúde/merenda; detalhar tudo é caro. Triagem barata por objeto escolhe o que detalhar.
**How to apply:** ao implementar `triar`, seguir estes estados/grupos e manter chave OpenRouter em variável de ambiente (`PNCP_OPENROUTER_API_KEY`), nunca no código. Ver [[coleta-incremental-pncp]] e [[pendencia-descarte-contratos-derivados]].


Implementado em 2026-11-13 (commit 258cf72): comando `triar`, `detalhar --triagem potencial`, tabela `compras` com triage_*; migracao aplicada no Railway. Smoke test real com 40 compras OK. Validacao fechada em 2026-11-13 (commit a4c418e): recall 8/9 'sim' com limiar 0,45 (padrao agora), 3 falsos positivos em 174, pares 40/40. Pendente: rodar `triar` completo na caixa (apos git pull e chave nova no .env) e depois `detalhar --triagem potencial`. To-do futuro: OCR dos PDFs para o estado `indefinido`.


2026-11-14: decisao de rodar o primeiro lote SO com compras de atas (`triar --origem ata`, ~144k compras, ~US$11), por orcamento e seguranca. Diego nao tem US$25 para o lote inteiro agora. Pasta experimentos/ removida do working tree; o branch `experimento/system-one-oss` guarda o relatorio.
