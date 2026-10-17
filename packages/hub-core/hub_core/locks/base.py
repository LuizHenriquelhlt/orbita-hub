"""Porta do lock distribuído.

Serve a um problema concreto: o agendador (cron) roda dentro do
`execution-service`, e o `execution-service` tem mais de uma réplica. Sem
exclusão mútua, um agendamento de 6h da manhã dispara uma vez por réplica.

A interface é mínima de propósito — `adquirir` e `liberar`, mais um gerenciador
de contexto. Tudo que um lock distribuído oferece além disso (reentrância,
renovação automática, fencing token) é funcionalidade que o chamador acaba
usando errado; quem precisa de garantia mais forte do que "no máximo um por
vez, com expiração" não deveria estar usando lock.
"""

from __future__ import annotations

import abc
import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


def chave_para_inteiro(nome: str) -> int:
    """Converte um nome de lock em um inteiro de 64 bits com sinal.

    Os *advisory locks* do PostgreSQL são identificados por `bigint`, não por
    texto. O hash precisa ser estável entre processos e entre versões do
    Python, o que descarta `hash()` embutido (randomizado por processo via
    PYTHONHASHSEED). blake2b com 8 bytes resolve.
    """
    digest = hashlib.blake2b(nome.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, byteorder="big", signed=True)


class Lock(abc.ABC):
    """Exclusão mútua entre processos."""

    @abc.abstractmethod
    async def adquirir(self, nome: str, *, ttl_segundos: int = 60) -> bool:
        """Tenta tomar o lock. Devolve `False` na hora se já estiver tomado.

        Não bloqueia por design: o chamador que quiser esperar escreve o laço
        de espera e decide o timeout, em vez de herdar um escondido aqui.
        """

    @abc.abstractmethod
    async def liberar(self, nome: str) -> None:
        """Devolve o lock. Idempotente: liberar o que não se tem não é erro."""

    @asynccontextmanager
    async def se_obtido(self, nome: str, *, ttl_segundos: int = 60) -> AsyncIterator[bool]:
        """Executa o bloco apenas se o lock foi obtido, e sempre o devolve.

        async with lock.se_obtido("cron:diario") as obtido:
            if not obtido:
                return
            ...
        """
        obtido = await self.adquirir(nome, ttl_segundos=ttl_segundos)
        try:
            yield obtido
        finally:
            if obtido:
                await self.liberar(nome)

    async def encerrar(self) -> None:
        """Libera recursos do adaptador (conexões, clientes)."""
        return None
