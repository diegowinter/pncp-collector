-- `itens_resultados.is_valid`: marca TODOS os resultados que valem como preco
-- (situacao "Informado" e valor unitario homologado > 0), 1:n por item. Antes so
-- existia `is_winner`, e a regra do vencedor nao olhava a situacao: um resultado
-- "Cancelado" (que costuma manter o preco antigo) podia ser marcado como vencedor.
--
-- O bloco de UPDATE recalcula `is_valid` e `is_winner` a partir das colunas ja
-- gravadas (mesma regra de detail_rules.is_valid_result / pick_winner), sem
-- consultar o PNCP. Idempotente: pode rodar de novo sempre que a regra mudar.

ALTER TABLE itens_resultados ADD COLUMN IF NOT EXISTS is_valid BOOLEAN NOT NULL DEFAULT false;
CREATE INDEX IF NOT EXISTS ix_itens_resultados_is_valid ON itens_resultados (is_valid);

-- `dataCancelamento` "0001-01-01" e campo vazio; o original continua em `raw`.
UPDATE itens_resultados SET data_cancelamento = NULL WHERE data_cancelamento < DATE '1900-01-01';

WITH regra AS (
    SELECT compra_key, numero_item, sequencial_resultado,
           (situacao_compra_item_resultado_nome = 'Informado'
            AND valor_unitario_homologado > 0) IS TRUE AS valido
    FROM itens_resultados
), classificado AS (
    SELECT g.compra_key, g.numero_item, g.sequencial_resultado, g.valido,
           g.valido AND ROW_NUMBER() OVER (
               PARTITION BY g.compra_key, g.numero_item, g.valido
               ORDER BY r.ordem_classificacao_srp NULLS LAST, g.sequencial_resultado
           ) = 1 AS vencedor
    FROM regra g
    JOIN itens_resultados r USING (compra_key, numero_item, sequencial_resultado)
)
UPDATE itens_resultados r
   SET is_valid = c.valido, is_winner = c.vencedor
  FROM classificado c
 WHERE (r.compra_key, r.numero_item, r.sequencial_resultado)
     = (c.compra_key, c.numero_item, c.sequencial_resultado)
   AND (r.is_valid, r.is_winner) IS DISTINCT FROM (c.valido, c.vencedor);
