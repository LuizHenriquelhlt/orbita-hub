"""Aplicação ASGI do `catalog-service`.

uvicorn hub_catalog.app:app --port 8002
"""

from __future__ import annotations

from fastapi import FastAPI

from hub_catalog.rotas import montar
from hub_core import auditoria, db
from hub_core.clients import IDENTITY, ClienteServico
from hub_core.service import criar_app
from hub_core.settings import obter_settings

DESCRICAO = """
Catálogo de automações: cadastro, versões, ciclo de vida, gatilhos e permissões
de acesso.

É a fonte da verdade sobre **o que existe e quem pode usar**. Não sabe nada
sobre execução — quantas vezes uma automação rodou ou se a última falhou vive
em `execution`. O portal compõe as duas coisas na tela.

Também é o dono do contrato de plugin: `GET /manifestos/schema` publica o JSON
Schema do `automation.yaml`, e `POST /manifestos/importar` registra uma
automação nova sem nenhuma alteração no código da plataforma.
"""


def construir() -> FastAPI:
    cfg = obter_settings()

    async def ao_iniciar(app: FastAPI) -> None:
        # O cliente é criado uma vez por processo e guardado no `app.state`.
        # Criar um por requisição descartaria o pool de conexões e
        # transformaria cada chamada em um handshake TCP novo.
        app.state.cliente_identity = ClienteServico(IDENTITY, cfg)
        auditoria.assinar_eventos(db.fabrica_de_sessoes(), servico="catalog")

    async def ao_encerrar(app: FastAPI) -> None:
        cliente = getattr(app.state, "cliente_identity", None)
        if cliente is not None:
            await cliente.aclose()

    return criar_app(
        nome="catalog",
        titulo="Órbita — Catálogo de Automações",
        descricao=DESCRICAO,
        router=montar(),
        settings=cfg,
        ao_iniciar=ao_iniciar,
        ao_encerrar=ao_encerrar,
    )


app = construir()
