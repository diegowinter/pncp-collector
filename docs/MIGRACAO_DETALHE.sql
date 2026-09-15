-- Migracao para a fase 2 (detalhamento) em banco que ja tem atas/contratos.
-- As tabelas novas (compras, itens, itens_resultados, arquivos) o
-- `pncp-collector init-db` cria sozinho (create_all); este script so cobre
-- as colunas novas nas tabelas existentes, que o create_all nao altera.
--
-- Alternativa, enquanto for base de teste: `pncp-collector reset-db --yes`
-- seguido de `pncp-collector all` (perde a fase 1).

ALTER TABLE atas
    ADD COLUMN IF NOT EXISTS detail_status   VARCHAR(30) NOT NULL DEFAULT 'pendente',
    ADD COLUMN IF NOT EXISTS detail_error    TEXT,
    ADD COLUMN IF NOT EXISTS detailed_at     TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS detail_attempts INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS compra_key      VARCHAR(80);

CREATE INDEX IF NOT EXISTS ix_atas_detail_status ON atas (detail_status);
CREATE INDEX IF NOT EXISTS ix_atas_compra_key    ON atas (compra_key);

ALTER TABLE contratos
    ADD COLUMN IF NOT EXISTS detail_status      VARCHAR(30) NOT NULL DEFAULT 'pendente',
    ADD COLUMN IF NOT EXISTS detail_error       TEXT,
    ADD COLUMN IF NOT EXISTS detailed_at        TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS detail_attempts    INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS compra_key         VARCHAR(80),
    ADD COLUMN IF NOT EXISTS compra_resolved_by VARCHAR(20);

CREATE INDEX IF NOT EXISTS ix_contratos_detail_status ON contratos (detail_status);
CREATE INDEX IF NOT EXISTS ix_contratos_compra_key    ON contratos (compra_key);
