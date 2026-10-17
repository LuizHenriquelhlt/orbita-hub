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
from hub_core.executor.reserva import ExecutorComReserva
from hub_core.executor.simulado import ExecutorSimulado
from hub_core.executor.subprocesso import ExecutorSubprocesso
from hub_core.settings import Settings

__all__ = [
    "PREFIXO_PROTOCOLO",
    "AoRegistrarLog",
    "ArtefatoProduzido",
    "ComandoDeProtocolo",
    "Executor",
    "ExecutorComReserva",
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

            principal: Executor = ExecutorDocker()
        case "subprocess":
            principal = ExecutorSubprocesso()
        case _:
            # O simulado nao precisa de reserva: ele ja e a reserva.
            return ExecutorSimulado()

    if settings.executor_fallback_simulado:
        from hub_core.executor.reserva import ExecutorComReserva

        return ExecutorComReserva(principal)
    return principal
