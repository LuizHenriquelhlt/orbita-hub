"""Execução de automações: porta, adaptadores e fábrica."""

from __future__ import annotations

from hub_core.executor.base import (
    PREFIXO_PROTOCOLO,
    AoRegistrarLog,
    ArtefatoProduzido,
    ComandoDeProtocolo,
    Executor,
    LimitesDeRecurso,
    PedidoDeExecucao,
    ResultadoExecucao,
    StatusExecucao,
    interpretar_linha,
)
from hub_core.executor.simulado import ExecutorSimulado
from hub_core.executor.subprocesso import ExecutorSubprocesso
from hub_core.settings import Settings

__all__ = [
    "PREFIXO_PROTOCOLO",
    "AoRegistrarLog",
    "ArtefatoProduzido",
    "ComandoDeProtocolo",
    "Executor",
    "ExecutorSimulado",
    "ExecutorSubprocesso",
    "LimitesDeRecurso",
    "PedidoDeExecucao",
    "ResultadoExecucao",
    "StatusExecucao",
    "construir_executor",
    "interpretar_linha",
]


def construir_executor(settings: Settings) -> Executor:
    """Escolhe o adaptador a partir de `EXECUTOR_BACKEND`."""
    match settings.executor_backend:
        case "docker":
            from hub_core.executor.docker_exec import ExecutorDocker

            return ExecutorDocker()
        case "subprocess":
            return ExecutorSubprocesso()
        case _:
            return ExecutorSimulado()
