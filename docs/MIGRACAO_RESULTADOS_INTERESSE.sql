-- Passo C filtrado pelo classificador (fase 5).
--
-- `itens.results_fetched_at`: quando o resultado do item foi buscado (NULL = ainda nao).
-- O controle passa a ser por item porque, com o filtro, so parte dos itens da compra e
-- consultada, e item consultado pode voltar sem nenhum resultado (nao deixa linha em
-- `itens_resultados`). ADD COLUMN sem default e instantaneo, mesmo com milhoes de linhas.
--
-- `ix_itens_predicted_interest`: a selecao do passo C e do passo A filtram por
-- `predicted_interest >= limiar` (~1,4% dos itens).
--
-- Idempotente.

ALTER TABLE itens ADD COLUMN IF NOT EXISTS results_fetched_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS ix_itens_predicted_interest ON itens (predicted_interest);

-- Banco que ja rodou o passo C antes desta coluna: item com resultado gravado ja foi buscado.
UPDATE itens i
   SET results_fetched_at = r.fetched_at
  FROM (SELECT compra_key, numero_item, MAX(collected_at) AS fetched_at
          FROM itens_resultados GROUP BY compra_key, numero_item) r
 WHERE i.compra_key = r.compra_key
   AND i.numero_item = r.numero_item
   AND i.results_fetched_at IS NULL;
