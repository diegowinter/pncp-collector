---
name: fase4-rotulagem-itens-treino
description: "Decisao de 2026-11-14 - a triagem de capa vira priorizador e fonte de treino; o corte real e por item, com Jev como rotulador de ~88 mil descricoes distintas e um classificador local treinado nelas"
metadata:
  node_type: memory
  type: project
  originSessionId: c83dc2b5-05e9-4231-b40c-55ede83452aa
  modified: 2026-11-14T15:28:45.908Z
---

Decidido com Diego em 2026-11-14, depois de medir falsos negativos da triagem de capa
(300 compras fora_por_ora sondadas: ~1 em 300 escondia item de interesse, sempre um item
solto em compra mista) e o custo real do passo B (itens): 56.570 compras em 2h55, sem
dinheiro, so API. Conclusao: baixar itens de TUDO e barato; o caro e o passo C
(resultados, 1 chamada por item) e classificar itens com modelo pago.

Desenho:
- Triagem de capa (Jev, fase 3) continua, mas so para ORDENAR a fila e gerar treino.
  Nada marcado fora_por_ora e descartado.
- Passo B em todas as compras com o tempo; `scripts/itens_treino.py` fez potenciais +
  indefinidas + 30 mil fora_por_ora de atas (2.470.697 itens, 1.656.271 descricoes distintas).
- Fase 4 (`triar-itens`, tabela `item_labels`): Jev rotula DESCRICOES DISTINTAS, nao linhas.
  Fatias: peneira (regex larga, 38.477 descricoes, todas), reserva (compra potencial fora
  da peneira, 10 mil), aleatorio (fora da peneira, 40 mil). Pergunta `categoria` (choice
  nas 14 categorias do pesquisa-de-precos-pncp + outros) e `interesse` (noul).
  Custo medido na fase 3: US$0,000079/chamada; conjunto inteiro US$7-11; Diego pos US$12.
- Depois: gabarito humano (~300 itens, pre-preenchido por Claude), treino local (TF-IDF+LR
  primeiro; CPU da maquina do Diego: Core Ultra 5, 14 nucleos, 15 GB, sem GPU), curva de
  aprendizado por classe decide se compra mais rotulos. Contratos (376 mil compras) so
  entram depois, direto pelo passo B + modelo, sem Jev na capa.

**Why:** classificador treinado so em compras potenciais nao aprende o "nao" parecido
(pistola de cola, coturno de obra); negativos vem das fora_por_ora. E o Jev paga rotulo
(uma vez), nao classificacao (a cada coleta).
**How to apply:** nao propor triagem de capa como filtro final; nao classificar itens em
massa com modelo pago. Ver [[fase3-triagem-objeto-jev]], [[experimento-system-one-oss]].

Achados da triagem de capa a corrigir no futuro: Exercito (CNPJ 00394452000103) entra como
potencial por "Regimento/Batalhao" mesmo comprando areia e expediente; regra "grupo >= 0,5
sozinho" leva outsourcing de impressao e ambulancia a potencial com potencial global 0,02-0,22
(~11% das potenciais). Exemplo de objeto vazio: 00394452000103-1-012013/2026 (Centro Hipico
-> farmacia veterinaria).

Resultados de 2026-11-14 (fim do dia):
- Lote Jev completo: 89.596 descricoes, US$10,53 (US$0,0001175/chamada), 0 erros. Contra as
  300 da planilha humana (pre-preenchida por Claude, Diego validou por amostragem): 269/274
  iguais, recall de interesse 51/51, 4 falsos positivos (colete nao operacional, macacao).
  Jev e liberal em "colete" -> vestuario_operacional.
- Peneira original tinha erro: "nas", "9 mm", "veiculo", "impressora" puxaram 115k; apertada
  para 49.596. Fatia aleatorio reduzida a 30k por orcamento.
- Baseline TF-IDF(word 1-2 + char_wb 3-5) + LogReg(C=8, balanced): acc 96%, macroF1 0,835 no
  holdout, curva ainda subindo (0,80 em 40k -> 0,835 em 80k). Recall alto, precisao baixa nas
  classes pequenas (protecao_balistica 0,46, vestuario 0,62, bicicleta 0,38). Contra as 300:
  95,3%, 14 discordancias, quase todas o modelo marcando a mais (coletes, pecas de veiculo).
  Treino em 157 s na CPU; prever 1,66M descricoes em 8,5 min.
- Aplicado as 1,66M descricoes: 58.007 itens de interesse (2,3%); passo C viavel (~58k chamadas).
- Falso negativo da capa medido: fora_por_ora 1.748/29.998 compras (5,8%) tem item marcado,
  934 com pmax>=0,8; indefinido 1.424/9.401; potencial so 8.592/17.169 (metade nao tem nada).
- Modelo e previsoes ficaram no scratchpad da sessao (nao versionados): proximo passo e
  formalizar `treinar-itens`/`prever-itens` no repo e gravar previsoes no banco.
- Reforco (US$1,43, saldo do Diego): classes raras estao ESGOTADAS no lake (a peneira ja pegou
  tudo; so 122 descricoes novas). Fatia `ativo` = 12.178 descricoes que o baseline marcava com
  duvida/exagero; Jev disse 78% outros (negativos dificeis). Total rotulado: 101.774.
- Retreino com objeto da compra como 3o bloco TF-IDF: v2 (balanced) planilha acc 0,960,
  interesse P=0,83 R=0,98; v3 (sem peso) acc 0,973, P=0,91 R=0,96 mas recall baixo nas raras
  (arma_nao_letal 0,55, bicicleta 0,50, manutencao 0,46). Recomendacao: v2 como filtro do
  passo C (recall), v3 (ou limiar por classe) para a categoria. Modelos no scratchpad.

2026-11-16: fase 5 formalizada no repo (`treinar-itens`, `prever-itens`, modelo em models/
fora do git, colunas predicted_* em itens). Passo B: atas 100% (143.908 compras), contratos
213.500/580.425. Previsao parou em 615.887/7.491.628 itens, retomavel pelo model_id
itens-20261116-145326. Diego esta migrando o processo para outro PC: dump em
D:\pncp-backup\pncp_railway_2026-11-16.dump (1,3 GB, pg_dump -Fc; disparado pelo PowerShell
porque pg_dump dentro do Bash da sessao perdia permissao de escrita no disco externo) mais
D:\pncp-backup\models (modelo treinado, para a previsao retomar sem retreinar). Pendente no
outro PC: restore, prever-itens, baixar_itens --origem contrato, e o filtro do passo C por
item aprovado (ainda nao implementado).
