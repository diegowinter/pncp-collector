"""Copia o banco do Railway para um Postgres local, tabela a tabela, por COPY em streaming.

Uso: PNCP_ORIGEM=<url psycopg> PNCP_DESTINO=<url psycopg> uv run python scripts/copiar_banco.py

Nao usa pg_dump (versao do cliente local pode ser menor que a do servidor). O
schema no destino e criado pelos modelos (create_all); os dados vao por
COPY ... TO STDOUT -> COPY ... FROM STDIN, em blocos, sem arquivo no disco.
Idempotente por tabela: tabela do destino com linhas e pulada.
"""

from __future__ import annotations

import os
import sys
import time

import psycopg
import psycopg.sql
from sqlalchemy import create_engine, text

from pncp_collector.models import Base

origem = os.environ["PNCP_ORIGEM"]
destino = os.environ["PNCP_DESTINO"]

engine = create_engine(destino.replace("postgresql://", "postgresql+psycopg://"))
Base.metadata.create_all(engine)

# Colunas por tabela a partir do modelo: COPY com lista explicita tolera ordem/colunas
# a mais no destino.
tables = [(t.name, [c.name for c in t.columns]) for t in Base.metadata.sorted_tables]

KEEPALIVE = dict(keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=5)
CHUNK = 50_000
RETRIES = 20


def connect_src() -> psycopg.Connection:
    conn = psycopg.connect(origem, **KEEPALIVE)
    conn.autocommit = True
    return conn


def single_pk(name: str) -> str | None:
    pk = [c.name for c in Base.metadata.tables[name].primary_key.columns]
    return pk[0] if len(pk) == 1 else None


def copy_stream(src: psycopg.Connection, dst: psycopg.Connection, select_sql: str, name: str, collist: str) -> None:
    with src.cursor() as sc, dst.cursor() as dc:
        with sc.copy(f"COPY ({select_sql}) TO STDOUT (FORMAT binary)") as out, dc.copy(
            f'COPY "{name}" ({collist}) FROM STDIN (FORMAT binary)'
        ) as inp:
            for data in out:
                inp.write(data)
    dst.commit()


src = connect_src()
with psycopg.connect(destino) as dst:
    for name, cols in tables:
        with src.cursor() as c:
            c.execute("select column_name from information_schema.columns where table_name=%s", (name,))
            existing = {r[0] for r in c.fetchall()}
        cols = [c for c in cols if c in existing]
        collist = ", ".join(f'"{c}"' for c in cols)
        pk = single_pk(name)
        t0 = time.time()

        with dst.cursor() as cur:
            cur.execute(f'select count(*) from "{name}"')
            have = cur.fetchone()[0]
        if have and pk is None:
            print(f"{name}: destino ja tem {have} linhas, pulando", flush=True)
            continue

        if pk is None:
            copy_stream(src, dst, f'select {collist} from "{name}"', name, collist)
        else:
            # Em lotes ordenados pela PK: retoma do maior valor ja gravado e sobrevive
            # a queda do proxy (reconecta e continua do ultimo lote confirmado).
            with dst.cursor() as cur:
                cur.execute(f'select max("{pk}") from "{name}"')
                last = cur.fetchone()[0]
            if have:
                print(f"{name}: retomando apos {last!r} ({have} linhas ja locais)", flush=True)
            attempt = 0
            while True:
                where = f'where "{pk}" > %s' if last is not None else ""
                select_sql = f'select {collist} from "{name}" {where} order by "{pk}" limit {CHUNK}'
                if last is not None:
                    with src.cursor() as c:
                        select_sql = c.mogrify(select_sql, (last,)) if hasattr(c, "mogrify") else select_sql.replace("%s", psycopg.sql.Literal(last).as_string(src))
                try:
                    copy_stream(src, dst, select_sql, name, collist)
                    attempt = 0
                except psycopg.OperationalError as exc:
                    attempt += 1
                    if attempt > RETRIES:
                        raise
                    dst.rollback()
                    print(f"{name}: conexao caiu ({str(exc).strip()[:60]}); tentativa {attempt} em {min(5 * attempt, 60)}s", flush=True)
                    time.sleep(min(5 * attempt, 60))
                    try:
                        src.close()
                    except Exception:
                        pass
                    src = connect_src()
                    continue
                with dst.cursor() as cur:
                    cur.execute(f'select max("{pk}"), count(*) from "{name}"')
                    new_last, have = cur.fetchone()
                if new_last == last:
                    break
                last = new_last
                print(f"{name}: {have} linhas... ({time.time() - t0:.0f}s)", flush=True)
        with dst.cursor() as cur:
            cur.execute(f'select count(*) from "{name}"')
            print(f"{name}: {cur.fetchone()[0]} linhas em {time.time() - t0:.0f}s", flush=True)

with engine.begin() as c:
    print("tamanho local:", c.execute(text("select pg_size_pretty(pg_database_size(current_database()))")).scalar())
print("concluido", flush=True)
