"""Roda a CLI do coletor com uma sonda de memoria em thread.

A cada N segundos grava em ~/memprobe.log: RSS, pico, tamanho do set de dedup,
quantos objetos o gc rastreia e o resumo do pymalloc (arenas alocadas vs bytes
realmente em uso). Se as arenas crescem e os bytes em uso nao, e fragmentacao.
"""
import gc
import os
import sys
import threading
import time

sys.path.insert(0, os.path.expanduser("~/pncp-collector/src"))

from pncp_collector import pipeline  # noqa: E402
from pncp_collector.cli import app  # noqa: E402

LOG = os.path.expanduser("~/memprobe.log")
INTERVAL = float(os.environ.get("MEMPROBE_INTERVAL", "20"))
state = {"seen": None, "pages": 0}

_orig_run = pipeline.Collector._run


def _run(self, *args, **kwargs):
    state["seen"] = kwargs.get("seen")
    return _orig_run(self, *args, **kwargs)


pipeline.Collector._run = _run

_orig_save = pipeline.save_day_progress


# MEMPROBE_TRIM=N chama malloc_trim(0) a cada N paginas, para testar se o
# crescimento e retencao da glibc (o trim segura o RSS) ou vazamento (nao segura).
TRIM_EVERY = int(os.environ.get("MEMPROBE_TRIM", "0"))


def malloc_trim():
    """Devolve ao sistema as paginas livres do heap da glibc (todas as arenas)."""
    import ctypes

    ctypes.CDLL("libc.so.6").malloc_trim(0)


def _save(*args, **kwargs):
    state["pages"] += 1
    if TRIM_EVERY and state["pages"] % TRIM_EVERY == 0:
        malloc_trim()
    return _orig_save(*args, **kwargs)


pipeline.save_day_progress = _save


def proc_status():
    out = {}
    with open("/proc/self/status") as f:
        for line in f:
            if line.startswith(("VmRSS", "VmHWM", "VmSwap", "VmData")):
                k, v = line.split(":", 1)
                out[k] = int(v.split()[0]) // 1024
    return out


def malloc_stats():
    """sys._debugmallocstats escreve no fd 2: desvia para um arquivo e le."""
    path = LOG + ".malloc"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    saved = os.dup(2)
    try:
        os.dup2(fd, 2)
        sys._debugmallocstats()
    finally:
        os.dup2(saved, 2)
        os.close(saved)
        os.close(fd)
    keep = ("# arenas allocated current", "# bytes in allocated blocks", "Total")
    with open(path) as f:
        lines = [l.strip() for l in f if l.startswith(keep)]
    return " | ".join(lines)


def glibc_stats():
    """mallinfo2: quanto a glibc segura no heap (arena) e quanto disso esta livre
    mas nao devolvido ao sistema (fordblks). Livre alto = fragmentacao/retencao."""
    import ctypes

    class MallInfo2(ctypes.Structure):
        _fields_ = [(n, ctypes.c_size_t) for n in (
            "arena", "ordblks", "smblks", "hblks", "hblkhd", "usmblks",
            "fsmblks", "uordblks", "fordblks", "keepcost")]

    libc = ctypes.CDLL("libc.so.6")
    libc.mallinfo2.restype = MallInfo2
    mi = libc.mallinfo2()
    return (f"glibc heap={mi.arena // 2**20}MB mmap={mi.hblkhd // 2**20}MB "
            f"used={mi.uordblks // 2**20}MB free_not_returned={mi.fordblks // 2**20}MB")


def probe():
    t0 = time.time()
    while True:
        time.sleep(INTERVAL)
        try:
            st = proc_status()
            seen = state["seen"]
            line = (
                f"{time.strftime('%H:%M:%S')} +{int(time.time() - t0):5d}s "
                f"rss={st.get('VmRSS')}MB hwm={st.get('VmHWM')}MB swap={st.get('VmSwap')}MB "
                f"pages={state['pages']} seen={len(seen) if seen is not None else '-'} "
                f"gc_objs={len(gc.get_objects())} threads={threading.active_count()} "
                f"|| {glibc_stats()} || {malloc_stats()}\n"
            )
            with open(LOG, "a") as f:
                f.write(line)
        except Exception as exc:  # a sonda nunca derruba a coleta
            with open(LOG, "a") as f:
                f.write(f"probe error: {exc!r}\n")


threading.Thread(target=probe, daemon=True).start()
app()
