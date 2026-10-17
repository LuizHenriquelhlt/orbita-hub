"""Fila de mensagens: porta, adaptadores e fábrica."""

from __future__ import annotations

from hub_core.broker.base import (
    FILA_INTERATIVA,
    FILA_LOTE,
    FILA_PADRAO,
    FILAS,
    Broker,
    Mensagem,
    backoff_segundos,
)
from hub_core.broker.postgres import BrokerPostgres
from hub_core.settings import Settings

__all__ = [
    "FILAS",
    "FILA_INTERATIVA",
    "FILA_LOTE",
    "FILA_PADRAO",
    "Broker",
    "BrokerPostgres",
    "Mensagem",
    "backoff_segundos",
    "construir_broker",
]


def construir_broker(settings: Settings) -> Broker:
    """Escolhe o adaptador a partir de `BROKER_BACKEND`.

    Quem chama recebe um `Broker` e não tem como descobrir qual é, que é
    exatamente o ponto. O `runner-service` tem um laço de consumo só.
    """
    if settings.broker_backend == "rabbitmq":
        from hub_core.broker.rabbitmq import BrokerRabbitMQ

        return BrokerRabbitMQ(
            settings.rabbitmq_url,
            max_tentativas=settings.fila_max_tentativas,
        )

    from hub_core.db import fabrica_de_sessoes

    return BrokerPostgres(
        fabrica_de_sessoes(),
        visibilidade_segundos=settings.fila_visibilidade_segundos,
        max_tentativas=settings.fila_max_tentativas,
    )
