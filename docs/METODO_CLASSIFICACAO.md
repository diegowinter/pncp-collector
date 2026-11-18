# Método de classificação de itens: técnicas e referências

Este documento responde a uma pergunta: o jeito como o projeto decide quais itens interessam
é uma técnica conhecida, e é boa prática? A resposta curta é sim. O desenho junta técnicas
clássicas de aprendizado de máquina, cada uma com nome e literatura próprios. Aqui fica o
mapa entre o que o código faz, como isso se chama e onde está descrito.

Escrito em 2026-11-18. As referências foram conferidas nessa data (título, autores, veículo e
link). Onde o texto diz "inferência nossa", a conclusão é do projeto e não está escrita na
fonte citada.

O funcionamento em si (rótulo, confiança, interesse, os três casos, o limiar no banco) está no
[README](../README.md), seção "Classificador local de itens (fase 5)".

## O desenho em uma frase

Um modelo grande e pago (Jev) rotula uma amostra de descrições; um modelo pequeno e local
aprende com esses rótulos e classifica todos os itens; a soma das chances das categorias de
interesse, comparada a um limiar, decide quais itens passam para a etapa cara (buscar o
resultado homologado no PNCP).

## Mapa: o que fazemos e como se chama

| O que o projeto faz | Nome da técnica | Referências |
|---|---|---|
| O Jev rotula 101.774 descrições e um modelo local aprende com elas (fases 4 e 5) | Destilação de conhecimento; LLM como rotulador | [1], [2] |
| O modelo devolve uma chance por categoria; o rótulo é a de maior chance | Classificação multiclasse probabilística | [3] |
| `predicted_interest` = soma das chances das 14 categorias de interesse = 1 − chance de `outros` | Regra da soma de probabilidades (marginalização) | ver nota abaixo |
| Um limiar sobre o interesse decide se o item entra no passo C, separado do rótulo | Limiar de decisão (ponto de operação) | [3], [4] |
| O filtro prefere errar para mais do que perder item bom | Classificação sensível a custo | [3], [4] |
| Classificador barato filtra antes da etapa cara (1 chamada por item) | Cascata, ou pipeline em estágios | [5], [6] |
| A fatia `ativo` manda para rotular as descrições em que o modelo local tem dúvida | Aprendizado ativo por amostragem de incerteza | [7], [8] |
| Medimos precisão e recall do interesse, não só o acerto geral | Avaliação por precisão e recall em dados desbalanceados | [9] |

Nota sobre a soma. As 15 categorias são mutuamente exclusivas e as chances somam 1. A chance
de "ser de alguma categoria de interesse" é, pela regra da soma, a soma das chances dessas
categorias. É probabilidade básica, não uma técnica com autor; por isso não tem referência
própria. Usar só a chance da categoria campeã para essa decisão é que estaria errado: ela
responde "qual é o item", não "o item é de interesse".

## O que cada referência sustenta

**Rotular com modelo grande e treinar um pequeno.** Hinton, Vinyals e Dean [1] propõem
transferir o conhecimento de um modelo grande para um pequeno, mais barato de usar. Wang e
colegas [2] medem o caso específico de usar um modelo de linguagem como rotulador: os rótulos
custam de 50% a 96% menos que rótulos humanos para o mesmo desempenho, e o modelo pequeno
treinado nesses rótulos pode superar o próprio rotulador. Diferença nossa: a destilação de [1]
ensina com as probabilidades do modelo grande (rótulos "suaves"); nós treinamos com a categoria
escolhida pelo Jev (rótulo "duro"), que é o caso estudado em [2]. As probabilidades do Jev
ficam guardadas em `item_labels.label_probabilities`, mas o treino não as usa.

**Separar a probabilidade da decisão.** O guia do scikit-learn [3] diz que a classificação é
melhor dividida em duas partes: o problema estatístico de prever probabilidades e o problema
de decisão de agir com base nelas. Diz também que o corte fixo em 0,5 "certamente não é o
ideal para a maioria dos casos de uso" e que o limiar deve seguir a métrica que importa para a
aplicação. O exemplo do guia é o nosso caso: quando perder um positivo custa mais que um
alarme falso, vale um limiar que privilegie o recall. É por isso que temos duas saídas
separadas (rótulo e interesse) e um limiar ajustável guardado no banco.

**Custo assimétrico.** Elkan [4] mostra que mudar a proporção das classes no treino equivale
a mudar o limiar de decisão, e vice-versa. No projeto, `class_weight=balanced` (que dá peso
às categorias raras) e o limiar de interesse são duas alavancas sobre a mesma coisa: quanto
recall trocar por precisão.

**Cascata.** Viola e Jones [5] tornaram conhecida a ideia de encadear classificadores: os
primeiros são baratos, rejeitam a maior parte dos negativos e mantêm quase todos os positivos;
só o que sobra chega às etapas caras. Wang, Lin e Metzler [6] aplicam o mesmo desenho à busca
de documentos. O nosso primeiro estágio é o classificador local (grátis, na CPU) e o estágio
caro é a consulta de resultado ao PNCP: o filtro reduz o passo C de 7,0 milhões para 96 mil
chamadas. A regra de projeto de uma cascata é a que seguimos: o estágio inicial deve ter
recall alto, porque o que ele rejeita não volta sozinho.

**Aprendizado ativo.** Lewis e Gale [7] introduziram a amostragem de incerteza: em vez de
rotular exemplos ao acaso, rotular aqueles em que o classificador atual está menos seguro, o
que reduz muito a quantidade de rótulos necessária. Settles [8] é o levantamento de referência
da área. A fatia `ativo` de `item_labels` (12.178 descrições) é isso.

**Medir com precisão e recall.** Itens de interesse são cerca de 1,4% dos itens. Saito e
Rehmsmeier [9] mostram que, com classes tão desbalanceadas, medidas como acerto geral e curva
ROC enganam, e que precisão e recall são mais informativos. Um modelo que respondesse `outros`
para tudo teria 98,6% de acerto e seria inútil. Por isso o número que acompanhamos é o par do
interesse: precisão 0,83 e recall 0,98 no holdout.

## O que já seguimos da boa prática

- **Gabarito humano fora do treino.** As 300 descrições conferidas por pessoa
  ([docs/validacao/](validacao/)) nunca entram no treino e servem só para medir. O guia [3]
  avisa para nunca ajustar limiar nos mesmos dados do treino.
- **Holdout de 10%** separado antes de treinar.
- **Limiar medido, não presumido.** O corte de 0,5 foi avaliado (recall 0,98 do interesse).
- **Primeiro estágio com recall alto**, como pede o desenho em cascata.
- **Decisão reversível.** O item abaixo do limiar fica no banco com a previsão gravada; mudar o
  limiar só acrescenta itens a buscar.

## Onde a prática recomendada vai além do que fizemos

- **Calibração.** Um classificador é bem calibrado quando, entre os itens a que ele dá 0,8,
  cerca de 80% são de fato positivos [10]. A regressão logística tende a ser bem calibrada
  [10], mas treinamos com pesos por classe, que alteram as proporções vistas pelo modelo. King
  e Zeng [12] tratam da correção necessária quando a proporção das classes no treino difere da
  real. Inferência nossa: o nosso `predicted_interest` está inflado para as categorias raras e
  não deve ser lido como chance exata; 0,5 é um ponto de operação que medimos, não "50% de
  chance". A correção padrão é calibrar com sigmoide (Platt) ou regressão isotônica em dados
  separados [10], [11]. Não medimos a curva de calibração do nosso modelo.
- **Escolher o limiar pela curva.** O usual é medir precisão e recall em vários limiares num
  conjunto de validação e escolher pelo custo [3]. Medimos só em 0,5. Fazer essa curva é o que
  responderia com dados se vale baixar o limiar para 0,4.
- **Amostrar o que ficou de fora.** Conferir de tempos em tempos uma amostra abaixo do limiar
  estima quantos itens bons escapam. Fizemos algo parecido uma vez, na triagem de capa (5,8%
  das compras `fora_por_ora` escondiam item de interesse); para itens, ainda não.

Nenhum desses pontos invalida o desenho. São refinamentos de medição.

## Referências

1. Hinton, G.; Vinyals, O.; Dean, J. (2015). *Distilling the Knowledge in a Neural Network*.
   NIPS 2014 Deep Learning Workshop. <https://arxiv.org/abs/1503.02531>
2. Wang, S.; Liu, Y.; Xu, Y.; Zhu, C.; Zeng, M. (2021). *Want To Reduce Labeling Cost? GPT-3
   Can Help*. Findings of EMNLP 2021. <https://arxiv.org/abs/2108.13487>
3. scikit-learn. *User Guide, 3.3. Tuning the decision threshold for class prediction*.
   <https://scikit-learn.org/stable/modules/classification_threshold.html>
4. Elkan, C. (2001). *The Foundations of Cost-Sensitive Learning*. IJCAI 2001, p. 973-978.
   <https://mlanthology.org/ijcai/2001/elkan2001ijcai-foundations>
5. Viola, P.; Jones, M. (2001). *Rapid Object Detection using a Boosted Cascade of Simple
   Features*. CVPR 2001. <https://www.cs.jhu.edu/~ayuille/courses/Stat238-Winter12/ViolaJones.pdf>
6. Wang, L.; Lin, J.; Metzler, D. (2011). *A Cascade Ranking Model for Efficient Ranked
   Retrieval*. SIGIR 2011, p. 105-114. <https://doi.org/10.1145/2009916.2009934>
7. Lewis, D. D.; Gale, W. A. (1994). *A Sequential Algorithm for Training Text Classifiers*.
   SIGIR 1994, p. 3-12. <https://arxiv.org/abs/cmp-lg/9407020>
8. Settles, B. (2009). *Active Learning Literature Survey*. Computer Sciences Technical Report
   1648, University of Wisconsin-Madison. <https://research.cs.wisc.edu/techreports/2009/TR1648.pdf>
9. Saito, T.; Rehmsmeier, M. (2015). *The Precision-Recall Plot Is More Informative than the
   ROC Plot When Evaluating Binary Classifiers on Imbalanced Datasets*. PLOS ONE 10(3):
   e0118432. <https://doi.org/10.1371/journal.pone.0118432>
10. scikit-learn. *User Guide, 1.16. Probability calibration*.
    <https://scikit-learn.org/stable/modules/calibration.html>
11. Niculescu-Mizil, A.; Caruana, R. (2005). *Predicting Good Probabilities with Supervised
    Learning*. ICML 2005, p. 625-632. <https://doi.org/10.1145/1102351.1102430>
12. King, G.; Zeng, L. (2001). *Logistic Regression in Rare Events Data*. Political Analysis
    9(2), p. 137-163. <https://gking.harvard.edu/publication/logistic-regression-in-rare-events-data/>

### Como as referências foram conferidas

- [3] e [10]: as páginas foram lidas; as frases atribuídas ao guia vêm delas. Nenhuma das duas
  fala de `class_weight`; a ligação entre peso por classe e calibração é inferência nossa,
  apoiada em [4] e [12].
- [1], [2], [7], [8], [9], [11]: existência, autores, veículo e resumo conferidos na fonte
  primária ou no registro oficial.
- [4], [5], [6], [12]: existência, autores e veículo conferidos; o conteúdo citado vem do
  resumo e de descrições secundárias, não da leitura integral do artigo.
