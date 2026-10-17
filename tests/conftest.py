"""Fixtures compartilhadas.

## Dois tipos de teste

A suíte é dividida por uma linha clara:

* **unitários**, regra pura, sem banco e sem rede. Rodam em milissegundos e
  são a maioria. Não precisam de nenhuma fixture daqui;
* **de integração** (`@pytest.mark.integracao`), exigem um PostgreSQL de
  verdade. Testam o que só se prova contra o banco: o `SKIP LOCKED` da fila, a
  imutabilidade da auditoria, o controle de admissão sob concorrência.

Os de integração são pulados automaticamente quando não há `DATABASE_URL`, em
vez de falharem. Quem clona o repositório e roda `pytest` vê a suíte unitária
passar sem precisar subir nada; o CI define a variável e roda tudo.

## Não aponte a suíte para um banco que tem runner rodando

Vale o aviso porque o sintoma engana. Se o `DATABASE_URL` dos testes for o
mesmo de um ambiente em execução, o worker daquele ambiente **consome as
mensagens que os testes publicam**: ele está em `BROKER_BACKEND=postgres`,
fazendo poll na mesma tabela, uma vez por segundo.

O resultado é uma suíte que falha de forma intermitente e em um teste
diferente a cada rodada, porque a ordem é aleatória (`pytest-randomly`) e a
mensagem roubada é a de quem estiver publicando na hora. Parece corrida entre
testes, e não é.

Use um banco só para teste, ou pare o ambiente antes: `make down`.

## Por que não SQLite nos testes de integração

Seria mais rápido e dispensaria o container. E inútil: `FOR UPDATE SKIP
LOCKED`, `make_interval`, `gen_random_uuid`, índice parcial, `JSONB` e
*advisory locks* não existem no SQLite. Um teste que passa no SQLite e falha
no PostgreSQL é pior do que teste nenhum, dá confiança sem dar garantia.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from hub_core.settings import Settings, obter_settings


def _url_de_teste() -> str | None:
    return os.getenv("DATABASE_URL")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Pula os testes de integração quando não há banco configurado."""
    del config
    if _url_de_teste():
        return
    motivo = pytest.mark.skip(
        reason="sem DATABASE_URL, defina-a para rodar os testes de integração "
        "(ver infra/docker-compose.yml)"
    )
    for item in items:
        if "integracao" in item.keywords:
            item.add_marker(motivo)


@pytest.fixture
def settings() -> Settings:
    """Settings isoladas do ambiente, com os adaptadores sem dependência externa."""
    obter_settings.cache_clear()
    return Settings(
        hub_mode="monolith",
        broker_backend="postgres",
        executor_backend="simulated",
        storage_backend="postgres",
        lock_backend="postgres",
        jwt_secret="segredo-de-teste-com-tamanho-suficiente-para-hmac",
        internal_shared_secret="segredo-interno-de-teste",
        database_url=_url_de_teste() or "postgresql+asyncpg://x:y@localhost/z",
        demo_mode=True,
    )


@pytest_asyncio.fixture
async def banco(settings: Settings) -> AsyncIterator[None]:
    """Inicializa o engine e o encerra ao fim do teste.

    Escopo de função, e não de sessão: cada teste de integração começa com um
    engine novo. É mais lento e evita a classe de falha mais irritante de
    suíte assíncrona, um engine preso a um event loop que já fechou.
    """
    from hub_core import db

    await db.encerrar()
    db.inicializar(settings)
    try:
        yield None
    finally:
        await db.encerrar()


@pytest_asyncio.fixture
async def limpar_fila(banco: None) -> AsyncIterator[None]:
    """Esvazia a tabela da fila antes e depois do teste.

    A fila é infraestrutura: truncá-la não destrói dado de negócio. Fazer isso
    **antes** também, e não só depois, protege contra lixo deixado por um teste
    anterior que falhou no meio.
    """
    del banco
    from sqlalchemy import text

    from hub_core import db

    async def truncar() -> None:
        async with db.fabrica_de_sessoes()() as sessao:
            await sessao.execute(text("TRUNCATE plataforma.fila_mensagens"))
            await sessao.commit()

    await truncar()
    try:
        yield None
    finally:
        await truncar()
