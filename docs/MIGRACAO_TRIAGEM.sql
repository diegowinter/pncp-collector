-- Fase 3 (triagem por objeto): colunas novas em `compras`. Idempotente.
-- Banco novo nao precisa: `init-db` cria tudo.

ALTER TABLE compras ADD COLUMN IF NOT EXISTS objeto TEXT;
ALTER TABLE compras ADD COLUMN IF NOT EXISTS objeto_source VARCHAR(10);
ALTER TABLE compras ADD COLUMN IF NOT EXISTS triage_status VARCHAR(20) NOT NULL DEFAULT 'pendente';
ALTER TABLE compras ADD COLUMN IF NOT EXISTS triage_clarity VARCHAR(20);
ALTER TABLE compras ADD COLUMN IF NOT EXISTS triage_potential NUMERIC(5, 4);
ALTER TABLE compras ADD COLUMN IF NOT EXISTS triage_groups JSONB;
ALTER TABLE compras ADD COLUMN IF NOT EXISTS triage_model TEXT;
ALTER TABLE compras ADD COLUMN IF NOT EXISTS triage_error TEXT;
ALTER TABLE compras ADD COLUMN IF NOT EXISTS triage_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE compras ADD COLUMN IF NOT EXISTS triaged_at TIMESTAMPTZ;
ALTER TABLE compras ADD COLUMN IF NOT EXISTS triage_raw JSONB;
CREATE INDEX IF NOT EXISTS ix_compras_triage_status ON compras (triage_status);
