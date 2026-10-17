"""Rotas HTTP do `runner-service`.

O runner é sobretudo um consumidor de fila, não um servidor HTTP. As rotas
existem para o que a fila não resolve:

* **cancelamento** é o único comando que precisa chegar a um runner
  *específico* — o que está rodando aquela execução. Mandar por fila não
  funciona: outro runner consumiria a mensagem e não teria o processo para
  interromper;
* **observabilidade** — situação do worker e containers gerenciados, para a
  área administrativa.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from hub_core.auth import Identidade, Permissao, ServicoChamador, exigir
from hub_core.errors import SemPermissao
from hub_core.logging import obter_logger

log = obter_logger(__name__)

router = APIRouter()
operacao = APIRouter(prefix="/execucoes", tags=["operação"])
administracao = APIRouter(tags=["administração"])


@operacao.post("/{execucao_id}/cancelar", summary="Interromper execução em andamento")
async def cancelar(
    execucao_id: str,
    request: Request,
    chamador: ServicoChamador,
) -> dict[str, Any]:
    """Exige token de serviço: é o `execution-service` que chama, não a pessoa.

    A pessoa cancela pelo `execution-service`, que registra a decisão na
    auditoria, muda o status e **depois** avisa o runner. Aceitar token de
    pessoa aqui permitiria interromper uma execução sem deixar rastro de quem
    pediu.
    """
    worker = getattr(request.app.state, "worker", None)
    if worker is None:
        return {"interrompida": False, "motivo": "runner sem worker ativo"}

    interrompida = await worker.cancelar(execucao_id)
    log.info("runner.cancelamento", execucao_id=execucao_id, de=chamador, atendido=interrompida)
    return {"interrompida": interrompida}


@administracao.get("/situacao", summary="Situação do worker")
async def situacao(
    request: Request,
    quem: Annotated[Identidade, Depends(exigir(Permissao.RUNNER_VER))],
) -> dict[str, Any]:
    del quem
    worker = getattr(request.app.state, "worker", None)
    executor = request.app.state.executor
    saudavel, detalhe = await executor.saudavel()

    return {
        "executor": executor.nome,
        "saudavel": saudavel,
        "detalhe": detalhe,
        "worker": worker.situacao() if worker is not None else None,
    }


@administracao.get("/containers", summary="Containers gerenciados pela plataforma")
async def containers(
    request: Request,
    quem: Annotated[Identidade, Depends(exigir(Permissao.RUNNER_VER))],
) -> list[dict[str, Any]]:
    """Só os containers com o rótulo da plataforma.

    O filtro por label está no `ExecutorDocker.listar_containers` e é o que
    impede esta tela de listar — e de oferecer um botão de parar para — o
    PostgreSQL da própria máquina de quem está desenvolvendo.
    """
    del quem
    executor = request.app.state.executor
    listar = getattr(executor, "listar_containers", None)
    if listar is None:
        # Com executor simulado ou de subprocesso não existem containers. Uma
        # lista vazia com explicação é melhor do que um 404 ou um erro.
        raise SemPermissao(
            f"o executor em uso ({executor.nome}) não gerencia containers",
            dica="use EXECUTOR_BACKEND=docker para habilitar esta área",
        )
    return await listar()


_montado = False


def montar() -> APIRouter:
    global _montado
    if not _montado:
        router.include_router(operacao)
        router.include_router(administracao)
        _montado = True
    return router
