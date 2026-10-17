"""Engine e sessões assíncronas.

A parte interessante deste módulo é `_montar_connect_args`: ele traduz o que um
PostgreSQL gerenciado exige para o que o asyncpg entende. Sem isso, a
aplicação funciona perfeitamente contra o PostgreSQL do docker-compose e falha
no primeiro deploy, duas vezes, por dois motivos diferentes.
"""

from __future__ import annotations

import ssl
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from hub_core.logging import obter_logger
from hub_core.settings import Settings, url_exige_ssl, url_usa_pooler

log = obter_logger(__name__)

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def _montar_connect_args(settings: Settings) -> dict[str, Any]:
    """Argumentos de conexão específicos do asyncpg.

    Dois ajustes, ambos aprendidos na prática:

    **TLS.** O asyncpg não aceita ``sslmode`` na URL; ele quer um
    ``ssl.SSLContext`` (ou ``True``) em ``connect_args``. Como `normalizar_
    database_url` já removeu o parâmetro, é aqui que a intenção é
    reconstituída. Usamos um contexto que verifica o certificado, Neon e
    Supabase apresentam cadeia pública válida, então não há motivo para
    desligar a verificação.

    **Prepared statements.** O endpoint *pooled* do Neon é um PgBouncer em modo
    transação, que não suporta *prepared statements* nomeados. O asyncpg os usa
    por padrão e, a partir da segunda query da conexão, a chamada estoura com
    ``prepared statement "__asyncpg_stmt_1__" does not exist``. A correção é
    ``statement_cache_size=0``; o custo é perder o cache de plano, aceitável
    para o volume de um portfólio e inevitável atrás de um pooler.
    """
    url_bruta = settings.url_banco_bruta
    args: dict[str, Any] = {}

    if url_exige_ssl(url_bruta):
        contexto = ssl.create_default_context()
        contexto.check_hostname = True
        contexto.verify_mode = ssl.CERT_REQUIRED
        args["ssl"] = contexto

    if url_usa_pooler(url_bruta):
        args["statement_cache_size"] = 0
        args["prepared_statement_cache_size"] = 0

    # O nome aparece em `pg_stat_activity`, o que torna possível descobrir qual
    # serviço abriu a conexão que está segurando um lock.
    args["server_settings"] = {"application_name": "orbita-hub"}

    return args


def criar_engine(settings: Settings) -> AsyncEngine:
    """Cria o engine. Chamado uma vez por processo, no lifespan do serviço."""
    connect_args = _montar_connect_args(settings)
    atras_de_pooler = url_usa_pooler(settings.url_banco_bruta)

    # Atrás de um pooler externo, manter um pool próprio é contraproducente:
    # são dois níveis de pool disputando o mesmo limite de conexões do plano
    # gratuito. NullPool deixa o PgBouncer fazer o trabalho dele.
    kwargs: dict[str, Any] = {
        "echo": settings.db_echo,
        "connect_args": connect_args,
        "pool_pre_ping": True,
        "future": True,
    }
    if atras_de_pooler:
        kwargs["poolclass"] = NullPool
    else:
        kwargs["pool_size"] = settings.db_pool_size
        kwargs["max_overflow"] = settings.db_max_overflow
        kwargs["pool_recycle"] = 1800

    log.info(
        "banco.engine_criado",
        pooler_externo=atras_de_pooler,
        tls="ssl" in connect_args,
    )
    return create_async_engine(settings.database_url, **kwargs)


def inicializar(settings: Settings) -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        return
    _engine = criar_engine(settings)
    _sessionmaker = async_sessionmaker(
        _engine,
        expire_on_commit=False,
        autoflush=False,
    )


async def encerrar() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


def engine() -> AsyncEngine:
    if _engine is None:
        raise RuntimeError("banco não inicializado, chame hub_core.db.inicializar(settings)")
    return _engine


def fabrica_de_sessoes() -> async_sessionmaker[AsyncSession]:
    if _sessionmaker is None:
        raise RuntimeError("banco não inicializado, chame hub_core.db.inicializar(settings)")
    return _sessionmaker


async def obter_sessao() -> AsyncIterator[AsyncSession]:
    """Dependência do FastAPI: uma sessão por requisição, com commit no fim.

    O `commit` fica aqui, e não espalhado pelos casos de uso, para que uma
    requisição seja uma transação. Um handler que precise de commit parcial
    abre uma sessão própria, e isso fica visível no código, como deve ser.
    """
    async with fabrica_de_sessoes()() as sessao:
        try:
            yield sessao
            await sessao.commit()
        except Exception:
            await sessao.rollback()
            raise
