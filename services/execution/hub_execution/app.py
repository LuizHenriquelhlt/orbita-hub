"""Aplicação ASGI do `execution-service`.

    uvicorn hub_execution.app:app --port 8003

Além das rotas, este serviço hospeda o **agendador** como tarefa de fundo. Ele
poderia ser um processo separado, e em uma instalação grande seria; aqui roda
junto porque o lock distribuído já garante que só uma réplica dispara cada
agendamento — então rodar o laço em todas as réplicas não causa disparo
duplicado, e dispensa um quinto artefato de deploy.
"""

from __future__ import annotations

import asyncio
import contextlib

from fastapi import FastAPI

from hub_core import auditoria, db
from hub_core.broker import construir_broker
from hub_core.clients import CATALOG, RUNNER, ClienteServico
from hub_core.locks import construir_lock
from hub_core.logging import obter_logger
from hub_core.service import criar_app
from hub_core.settings import obter_settings
from hub_core.storage import construir_storage
from hub_execution import agendador
from hub_execution.rotas import montar

log = obter_logger(__name__)

DESCRICAO = """
Motor de execução: disparo, fila, controle de admissão, acompanhamento, log ao
vivo, artefatos, reexecução e agendamento.

**Controle de admissão** usa lock distribuído para que o limite de execuções
simultâneas de uma automação valha com várias réplicas do serviço — contar e
inserir precisam ser atômicos entre processos.

**As rotas sob `/_internos`** são os callbacks do `runner-service` e exigem
token de serviço, não token de pessoa.
"""


def construir() -> FastAPI:
    cfg = obter_settings()
    parar = asyncio.Event()

    async def ao_iniciar(app: FastAPI) -> None:
        app.state.cliente_catalog = ClienteServico(CATALOG, cfg)
        app.state.cliente_runner = ClienteServico(RUNNER, cfg, timeout=10.0)

        app.state.broker = construir_broker(cfg)
        await app.state.broker.iniciar()

        app.state.storage = construir_storage(cfg)
        await app.state.storage.iniciar()

        app.state.lock = construir_lock(cfg)

        auditoria.assinar_eventos(db.fabrica_de_sessoes(), servico="execution")

        app.state.tarefa_agendador = asyncio.create_task(
            agendador.laco(
                fabrica=db.fabrica_de_sessoes(),
                settings=cfg,
                cliente_catalog=app.state.cliente_catalog,
                broker=app.state.broker,
                lock=app.state.lock,
                parar=parar,
            ),
            name="agendador",
        )

    async def ao_encerrar(app: FastAPI) -> None:
        # Sinalizar antes de cancelar dá ao laço a chance de terminar a volta
        # atual. Cancelar direto no meio de um disparo deixaria a execução
        # gravada sem mensagem na fila — recuperável, mas evitável.
        parar.set()
        tarefa = getattr(app.state, "tarefa_agendador", None)
        if tarefa is not None:
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(tarefa, timeout=10)
            if not tarefa.done():
                tarefa.cancel()

        for atributo in ("cliente_catalog", "cliente_runner"):
            cliente = getattr(app.state, atributo, None)
            if cliente is not None:
                await cliente.aclose()

        for atributo in ("broker", "storage", "lock"):
            recurso = getattr(app.state, atributo, None)
            if recurso is not None:
                await recurso.encerrar()

    return criar_app(
        nome="execution",
        titulo="Órbita — Motor de Execução",
        descricao=DESCRICAO,
        router=montar(),
        settings=cfg,
        ao_iniciar=ao_iniciar,
        ao_encerrar=ao_encerrar,
    )


app = construir()
