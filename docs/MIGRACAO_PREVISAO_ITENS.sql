-- Fase 5 (classificador local de itens): colunas de previsao em `itens`. Idempotente.
-- Banco novo nao precisa: `init-db` cria tudo.

ALTER TABLE itens ADD COLUMN IF NOT EXISTS predicted_category VARCHAR(40);
ALTER TABLE itens ADD COLUMN IF NOT EXISTS predicted_confidence NUMERIC(5, 4);
ALTER TABLE itens ADD COLUMN IF NOT EXISTS predicted_interest NUMERIC(5, 4);
ALTER TABLE itens ADD COLUMN IF NOT EXISTS predicted_model VARCHAR(80);
ALTER TABLE itens ADD COLUMN IF NOT EXISTS predicted_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS ix_itens_predicted_category ON itens (predicted_category);
