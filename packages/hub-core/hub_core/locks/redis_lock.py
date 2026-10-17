"""Lock distribuído sobre Redis.

Adaptador usado quando há Redis no ambiente (docker-compose local, Kubernetes).
É a implementação clássica `SET chave valor NX PX ttl`.

O detalhe que separa uma implementação correta de uma com bug de corrida é a
**liberação**: um `DEL` simples apaga o lock mesmo quando ele já expirou e foi
retomado por outro processo — o processo lento apaga o lock do rápido. A
proteção é guardar um token aleatório no valor e só apagar se o valor conferir,
o que exige comparar-e-apagar atomicamente. Em Redis, isso é um script Lua.

Este adaptador não implementa Redlock (quórum entre vários nós). Com um único
nó Redis, um failover pode perder o lock — risco aceito aqui e registrado em
docs/adr/0004. Para o uso do projeto (evitar disparo duplicado de agendamento),
o pior caso é uma execução repetida, que o motor já trata por idempotência.
"""

from __future__ import annotations

import secrets
from typing import Any

from hub_core.locks.base import Lock
from hub_core.logging import obter_logger

log = obter_logger(__name__)

# KEYS[1] = chave do lock, ARGV[1] = token de quem acha que detém o lock.
_LUA_LIBERAR = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
else
    return 0
end
"""


class LockRedis(Lock):
    def __init__(self, url: str) -> None:
        try:
            from redis.asyncio import Redis
        except ImportError as erro:  # pragma: no cover
            raise RuntimeError(
                "LOCK_BACKEND=redis exige o extra 'redis': pip install 'orbita-hub[redis]'"
            ) from erro

        self._redis: Any = Redis.from_url(url, decode_responses=True)
        self._tokens: dict[str, str] = {}
        self._liberar = self._redis.register_script(_LUA_LIBERAR)

    @staticmethod
    def _chave(nome: str) -> str:
        return f"orbita:lock:{nome}"

    async def adquirir(self, nome: str, *, ttl_segundos: int = 60) -> bool:
        if nome in self._tokens:
            return False

        token = secrets.token_urlsafe(16)
        obtido = await self._redis.set(self._chave(nome), token, nx=True, px=ttl_segundos * 1000)
        if not obtido:
            return False

        self._tokens[nome] = token
        log.debug("lock.obtido", nome=nome, backend="redis", ttl=ttl_segundos)
        return True

    async def liberar(self, nome: str) -> None:
        token = self._tokens.pop(nome, None)
        if token is None:
            return
        await self._liberar(keys=[self._chave(nome)], args=[token])
        log.debug("lock.liberado", nome=nome, backend="redis")

    async def encerrar(self) -> None:
        for nome in list(self._tokens):
            await self.liberar(nome)
        await self._redis.aclose()
