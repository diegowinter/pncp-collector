-- Campos de texto livre que estavam em VARCHAR(120) e estouravam com payloads
-- reais do PNCP (`processo` com a descricao inteira do pregao -> DataError e a
-- varredura caia na mesma pagina a cada retomada).
-- So metadado: o Postgres nao reescreve a tabela ao alargar para TEXT. Idempotente.

ALTER TABLE contratos ALTER COLUMN processo TYPE TEXT;
ALTER TABLE contratos ALTER COLUMN numero_contrato_empenho TYPE TEXT;
ALTER TABLE atas ALTER COLUMN numero_ata_registro_preco TYPE TEXT;
