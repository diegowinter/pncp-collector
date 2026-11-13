"""Aplica docs/MIGRACAO_NI_FORNECEDOR.sql no banco do .env.

Nao chama a API: o payload inteiro ja esta em `raw`, entao so alarga a coluna
e refaz `ni_fornecedor` nas linhas que foram cortadas em 30 caracteres.
Mostra antes quantas linhas serao corrigidas; com --dry-run para ai.

    uv run python scripts/patch_ni_fornecedor.py [--dry-run]
"""
import sys
from pathlib import Path

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pncp_collector.config import settings  # noqa: E402
from pncp_collector.db import build_engine  # noqa: E402

SQL = Path(__file__).resolve().parents[1] / "docs" / "MIGRACAO_NI_FORNECEDOR.sql"
COUNT = """
SELECT count(*) FROM {table}
 WHERE raw->>'niFornecedor' IS NOT NULL
   AND ni_fornecedor IS DISTINCT FROM raw->>'niFornecedor'
"""


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    engine = build_engine(settings)
    with engine.begin() as conn:
        for table in ("contratos", "itens_resultados"):
            n = conn.execute(text(COUNT.format(table=table))).scalar_one()
            print(f"{table}: {n} linha(s) com ni_fornecedor divergente de raw")
        if dry_run:
            print("--dry-run: nada alterado.")
            return
        for stmt in SQL.read_text(encoding="utf-8").split(";"):
            if stmt.strip() and not all(l.strip().startswith("--") for l in stmt.strip().splitlines()):
                conn.execute(text(stmt))
    print("Migracao aplicada.")


if __name__ == "__main__":
    main()
