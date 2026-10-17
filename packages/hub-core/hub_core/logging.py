"""Logging estruturado.

Em desenvolvimento sai colorido e legível; em produção sai como um JSON por
linha, que é o que qualquer coletor (Cloud Logging, Loki, ELK) lê sem parser
customizado.

O campo que importa é `trace_id`: ele é propagado por `contextvars`, então um
log emitido no fundo do executor carrega o id da execução que o originou sem
que ninguém tenha precisado passar o parâmetro de função em função. É o que
permite responder "o que aconteceu na execução X" com um único filtro.
"""

from __future__ import annotations

import logging
import sys
from contextvars import ContextVar
from typing import Any

import structlog

_trace_id: ContextVar[str | None] = ContextVar("trace_id", default=None)


def definir_trace_id(valor: str | None) -> None:
    _trace_id.set(valor)


def obter_trace_id() -> str | None:
    return _trace_id.get()


def _injeta_trace_id(_logger: Any, _nome: str, evento: dict[str, Any]) -> dict[str, Any]:
    tid = _trace_id.get()
    if tid:
        evento["trace_id"] = tid
    return evento


def configurar_logging(*, json: bool = False, nivel: str = "INFO") -> None:
    nivel_num = logging.getLevelNamesMapping().get(nivel.upper(), logging.INFO)
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=nivel_num)

    processadores: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        _injeta_trace_id,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    processadores.append(
        structlog.processors.JSONRenderer() if json else structlog.dev.ConsoleRenderer()
    )

    structlog.configure(
        processors=processadores,
        wrapper_class=structlog.make_filtering_bound_logger(nivel_num),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def obter_logger(nome: str) -> Any:
    return structlog.get_logger(nome)
