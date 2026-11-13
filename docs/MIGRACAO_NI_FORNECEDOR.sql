-- `ni_fornecedor` era VARCHAR(30) pensando em CNPJ/CPF, mas fornecedor
-- estrangeiro chega como "ESTRANGEIRO_<NOME DA EMPRESA>" e passava do limite:
-- o valor era cortado com warning (schemas._fit) e o original ficou so em `raw`.
-- Alarga para TEXT (so metadado) e reconstroi o valor a partir de `raw`.
-- Idempotente: a segunda rodada nao encontra linha para corrigir.

ALTER TABLE contratos ALTER COLUMN ni_fornecedor TYPE TEXT;
ALTER TABLE itens_resultados ALTER COLUMN ni_fornecedor TYPE TEXT;

UPDATE contratos
   SET ni_fornecedor = raw->>'niFornecedor'
 WHERE raw->>'niFornecedor' IS NOT NULL
   AND ni_fornecedor IS DISTINCT FROM raw->>'niFornecedor';

UPDATE itens_resultados
   SET ni_fornecedor = raw->>'niFornecedor'
 WHERE raw->>'niFornecedor' IS NOT NULL
   AND ni_fornecedor IS DISTINCT FROM raw->>'niFornecedor';
