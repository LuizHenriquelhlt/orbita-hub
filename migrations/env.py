"""Ambiente de migração do Alembic.

Duas particularidades deste projeto que o `env.py` padrão não cobre:

**Cinco `MetaData`, não uma.** Cada serviço tem a sua `DeclarativeBase` presa a
um schema (ver `hub_core.db.base.criar_base`). O Alembic aceita uma sequência
em `target_metadata`, e é isso que permite um `autogenerate` só ver os cinco
schemas. Sem `include_schemas=True`, ele compararia apenas o schema `public` e
proporia apagar tudo que existe.

**Engine assíncrono.** O projeto usa `asyncpg`, e o Alembic é síncrono. A ponte
é `connection.run_sync`, que roda a função de migração dentro do greenlet do
SQLAlchemy — a forma suportada, e não um `asyncio.run` por fora, que colidiria
com o event loop.
"""

from __future__ import annotations

import asyncio
import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import pool, text
from sqlalchemy.ext.asyncio import async_engine_from_config

RAIZ = Path(__file__).resolve().parents[1]
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

# Os imports abaixo existem pelo efeito colateral: registram as tabelas nas
# respectivas `MetaData`. Sem eles, `autogenerate` acharia o banco inteiro
# sobrando e geraria uma migração que apaga tudo.
from hub_catalog import modelos as modelos_catalog  # noqa: E402
from hub_core.db import plataforma as modelos_plataforma  # noqa: E402
from hub_core.db.base import SCHEMAS  # noqa: E402
from hub_core.settings import normalizar_database_url  # noqa: E402
from hub_execution import modelos as modelos_execution  # noqa: E402
from hub_identity import modelos as modelos_identity  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = [
    modelos_identity.Base.metadata,
    modelos_catalog.Base.metadata,
    modelos_execution.Base.metadata,
    modelos_plataforma.BasePlataforma.metadata,
]


def _url() -> str:
    """URL do banco, em versão **síncrona** para o Alembic offline.

    O modo online usa asyncpg; o offline (`--sql`, que só gera o SQL) não abre
    conexão, então o driver é irrelevante — mas a URL precisa ser parseável.
    """
    bruta = os.getenv("DATABASE_URL")
    if not bruta:
        raise RuntimeError(
            "DATABASE_URL não definida. Exporte-a ou use `make migrar`, que a lê do .env."
        )
    return normalizar_database_url(bruta)


def incluir_objeto(objeto: object, nome: str | None, tipo: str, *_args: object) -> bool:
    """Filtra o que o `autogenerate` considera.

    Sem este filtro, o Alembic enxerga a própria `alembic_version` e qualquer
    extensão instalada no banco como tabelas sobrando, e propõe removê-las.
    """
    if tipo == "table":
        schema = getattr(objeto, "schema", None)
        if schema not in SCHEMAS:
            return False
        if nome == "alembic_version":
            return False
    return True


def executar_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        include_schemas=True,
        include_object=incluir_objeto,
        version_table_schema="public",
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _migrar(connection: object) -> None:
    context.configure(
        connection=connection,  # type: ignore[arg-type]
        target_metadata=target_metadata,
        include_schemas=True,
        include_object=incluir_objeto,
        # A tabela de controle fica em `public`: ela é do banco, não de um
        # serviço. Posta em um dos schemas de domínio, daria a impressão de
        # que aquele serviço é o dono das migrações de todos.
        version_table_schema="public",
        compare_type=True,
        compare_server_default=True,
        # Uma transação por migração: ou a migração inteira é aplicada, ou
        # nada dela. Com DDL transacional (que o PostgreSQL tem, e a maioria
        # dos bancos não), isso é de graça — e evita o pior cenário de
        # migração, que é o banco parar no meio.
        transaction_per_migration=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def executar_online_async() -> None:
    configuracao = config.get_section(config.config_ini_section, {})
    configuracao["sqlalchemy.url"] = _url()

    engine = async_engine_from_config(
        configuracao,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        connect_args=_connect_args(),
    )

    async with engine.connect() as conexao:
        # Os schemas precisam existir antes de a primeira migração rodar: o
        # Alembic cria tabelas, não schemas. Fica aqui, e não na migração
        # inicial, para que um banco novo funcione em qualquer ponto do
        # histórico de migrações.
        for schema in SCHEMAS:
            await conexao.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
        await conexao.commit()

        await conexao.run_sync(_migrar)

    await engine.dispose()


def _connect_args() -> dict[str, object]:
    """TLS e cache de statements, para quando o alvo é um PostgreSQL gerenciado."""
    from hub_core.settings import url_exige_ssl, url_usa_pooler

    bruta = os.getenv("DATABASE_URL", "")
    args: dict[str, object] = {}
    if url_exige_ssl(bruta):
        import ssl

        args["ssl"] = ssl.create_default_context()
    if url_usa_pooler(bruta):
        args["statement_cache_size"] = 0
    return args


if context.is_offline_mode():
    executar_offline()
else:
    asyncio.run(executar_online_async())
