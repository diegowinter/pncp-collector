-- Tabela `settings`: parametros de operacao guardados no banco (chave -> valor em
-- texto), para mudar sem mexer em codigo nem em `.env`, valer em toda maquina que
-- usa o mesmo banco e poder ser editado por um painel.
--
-- Primeiro parametro: `item_interest_threshold`, o limiar de `predicted_interest` a
-- partir do qual o item entra no passo C. Semeado com 0.5 (o padrao do codigo); se a
-- linha ja existe, nada muda. Mudar: `uv run pncp-collector limiar 0.4`, ou UPDATE.
--
-- Qualquer comando que abre o banco pelo `create_all` tambem cria a tabela; este
-- arquivo existe para quem aplica as migracoes a mao. Idempotente.

CREATE TABLE IF NOT EXISTS settings (
    key         VARCHAR(60) PRIMARY KEY,
    value       TEXT NOT NULL,
    description TEXT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO settings (key, value, description)
VALUES (
    'item_interest_threshold',
    '0.5',
    'Limiar de predicted_interest (soma das chances das categorias de interesse) a partir do qual o item entra no passo C. Entre 0 e 1.'
)
ON CONFLICT (key) DO NOTHING;
