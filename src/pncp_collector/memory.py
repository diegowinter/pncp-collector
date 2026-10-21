"""Devolve ao sistema a memoria que o malloc da glibc segura sem usar.

Medido na TV box (armv7l, 1 GB): o RSS do coletor subia ~0,4 MB por pagina
ate o OOM killer, enquanto o heap do Python (pymalloc) e o numero de objetos
ficavam estaveis. A diferenca era heap da glibc: as threads de download e a
thread principal alocam e liberam blocos grandes (respostas de ~1 MB, JSON,
texto dos inserts) em arenas diferentes, e a glibc nao devolve ao kernel as
paginas que ficam livres no meio do heap. `malloc_trim(0)` devolve (via
madvise) e, chamado periodicamente, segurou o RSS em ~90 MB onde antes
passava de 600 MB. Fora do Linux/glibc e um no-op.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from collections.abc import Callable

logger = logging.getLogger(__name__)

_trim: Callable[[int], int] | None = None
_loaded = False


def _load() -> Callable[[int], int] | None:
    if sys.platform != "linux":
        return None
    try:
        libc = ctypes.CDLL("libc.so.6")
        return libc.malloc_trim
    except (OSError, AttributeError) as exc:  # musl, por exemplo
        logger.debug("malloc_trim indisponivel: %s", exc)
        return None


def release_heap() -> bool:
    """Chama malloc_trim(0). Devolve True se chamou."""
    global _trim, _loaded
    if not _loaded:
        _trim = _load()
        _loaded = True
    if _trim is None:
        return False
    _trim(0)
    return True
