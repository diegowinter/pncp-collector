-- Fase 4 (rotulagem de itens): tabela `item_labels`. Idempotente.
-- Banco novo nao precisa: `init-db` (ou qualquer comando) cria tudo via create_all.

CREATE TABLE IF NOT EXISTS item_labels (
    descricao_norm            TEXT PRIMARY KEY,
    descricao                 TEXT NOT NULL,
    compra_key                VARCHAR(80),
    objeto                    TEXT,
    unidade_medida            TEXT,
    valor_unitario_estimado   NUMERIC(18, 4),
    material_ou_servico_nome  VARCHAR(40),
    item_count                INTEGER NOT NULL DEFAULT 1,
    sample_slice              VARCHAR(20) NOT NULL,
    label_status              VARCHAR(20) NOT NULL DEFAULT 'pendente',
    label_category            VARCHAR(40),
    label_confidence          NUMERIC(5, 4),
    label_interest            NUMERIC(5, 4),
    label_probabilities       JSONB,
    label_model               TEXT,
    label_error               TEXT,
    label_attempts            INTEGER NOT NULL DEFAULT 0,
    labeled_at                TIMESTAMPTZ,
    label_raw                 JSONB,
    collected_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_item_labels_compra_key ON item_labels (compra_key);
CREATE INDEX IF NOT EXISTS ix_item_labels_sample_slice ON item_labels (sample_slice);
CREATE INDEX IF NOT EXISTS ix_item_labels_label_status ON item_labels (label_status);
CREATE INDEX IF NOT EXISTS ix_item_labels_label_category ON item_labels (label_category);
