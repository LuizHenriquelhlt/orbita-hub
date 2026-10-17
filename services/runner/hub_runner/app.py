"""Aplicação ASGI do `runner-service`.

    uvicorn hub_runner.app:app --port 8004

Este é o serviço que tem mais privilégio da plataforma: com
`EXECUTOR_BACKEND=docker`, ele fala com o Docker Engine, e quem fala com o
Docker Engine é efetivamente root na máquina. Daí duas consequências de
desenho:

* ele **não** tem acesso ao banco de domínio dos outros serviços — reporta
  tudo por HTTP, com token de serviço;
* é o único lugar onde os valores de segredo das automações existem, resolvidos
  do próprio ambiente no momento de montar o container.

Em Kubernetes, é o único Pod que precisaria de `ServiceAccount` com
permissão para criar Jobs — e por isso o que ele pode fazer deve ser auditado
com mais cuidado do que o resto.
"""

from __future__ import annotations

import asyncio
import contextlib
import os

from fastapi import FastAPI

from hub_core.broker import construir_broker
from hub_core.clients import EXECUTION, ClienteServico
from hub_core.executor import construir_executor
from hub_core.logging import obter_logger
from hub_core.service import criar_app
from hub_core.settings import obter_settings
from hub_core.storage import construir_storage
from hub_runner.rotas import montar
from hub_runner.worker import Worker

log = obter_logger(__name__)

DESCRICAO = """
Consome a fila de execuções e roda as automações.

Suporta três executores, escolhidos por `EXECUTOR_BACKEND`:

| Executor | Isola | Onde roda |
|---|---|---|
| `docker` | cgroups, namespace de rede, raiz somente leitura | produção |
| `subprocess` | ambiente e sistema de arquivos de trabalho | desenvolvimento |
| `simulated` | nada (reproduz um roteiro) | demonstração pública |

O executor usado é persistido em cada execução e aparece na interface — uma
execução simulada é rotulada como tal.
"""


def construir() -> FastAPI:
    cfg = obter_settings()
    parar = asyncio.Event()

    # O worker pode ser desligado para rodar o runner apenas como serviço HTTP
    # (útil para separar quem consome a fila de quem atende cancelamento, em
    # uma instalação com muitas réplicas).
    consumir = os.getenv("RUNNER_CONSOME_FILA", "true").lower() not in {"false", "0", "no"}
    max_concorrentes = int(os.getenv("RUNNER_MAX_CONCORRENTES", "2"))

    async def ao_iniciar(app: FastAPI) -> None:
        app.state.cliente_execution = ClienteServico(EXECUTION, cfg)

        app.state.executor = construir_executor(cfg)
        await app.state.executor.iniciar()
        saudavel, detalhe = await app.state.executor.saudavel()
        log.info(
            "runner.executor", nome=app.state.executor.nome, saudavel=saudavel, detalhe=detalhe
        )

        app.state.storage = construir_storage(cfg)
        await app.state.storage.iniciar()

        app.state.broker = construir_broker(cfg)
        await app.state.broker.iniciar()

        if consumir:
            app.state.worker = Worker(
                broker=app.state.broker,
                executor=app.state.executor,
                storage=app.state.storage,
                cliente_execution=app.state.cliente_execution,
                settings=cfg,
                max_concorrentes=max_concorrentes,
            )
            app.state.tarefa_worker = asyncio.create_task(
                app.state.worker.laco(parar), name="worker"
            )
        else:
            app.state.worker = None
            log.info("runner.sem_consumo", motivo="RUNNER_CONSOME_FILA=false")

    async def ao_encerrar(app: FastAPI) -> None:
        # Sinalizar e esperar: o worker termina a execução em andamento antes
        # de sair. O que não terminar volta à fila pelo *visibility timeout*,
        # então nada é perdido — mas terminar de forma limpa evita uma
        # execução contada duas vezes.
        parar.set()
        tarefa = getattr(app.state, "tarefa_worker", None)
        if tarefa is not None:
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(tarefa, timeout=40)
            if not tarefa.done():
                tarefa.cancel()

        cliente = getattr(app.state, "cliente_execution", None)
        if cliente is not None:
            await cliente.aclose()

        for atributo in ("broker", "storage", "executor"):
            recurso = getattr(app.state, atributo, None)
            if recurso is not None:
                await recurso.encerrar()

    return criar_app(
        nome="runner",
        titulo="Órbita — Runner de Automações",
        descricao=DESCRICAO,
        router=montar(),
        settings=cfg,
        ao_iniciar=ao_iniciar,
        ao_encerrar=ao_encerrar,
    )


app = construir()
