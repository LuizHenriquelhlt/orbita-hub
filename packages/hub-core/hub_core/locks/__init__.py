"""Lock distribuído: porta, adaptadores e fábrica."""

from __future__ import annotations

from hub_core.locks.base import Lock, chave_para_inteiro
from hub_core.locks.postgres import LockPostgres
from hub_core.settings import Settings

__all__ = ["Lock", "LockPostgres", "chave_para_inteiro", "construir_lock"]


def construir_lock(settings: Settings) -> Lock:
    """Escolhe o adaptador a partir de `LOCK_BACKEND`.

    O import do Redis é tardio de propósito: quem roda no free tier não tem o
    pacote instalado, e um import no topo do módulo faria o processo nem subir.
    """
    if settings.lock_backend == "redis":
        from hub_core.locks.redis_lock import LockRedis

        return LockRedis(settings.redis_url)

    from hub_core.db import engine

    return LockPostgres(engine())
