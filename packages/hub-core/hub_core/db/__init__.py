"""Acesso ao PostgreSQL: engine, sessões e a base do mapeamento."""

from hub_core.db.base import (
    CONVENCAO_DE_NOMES,
    SCHEMAS,
    IdMixin,
    TimestampMixin,
    criar_base,
)
from hub_core.db.engine import (
    criar_engine,
    encerrar,
    engine,
    fabrica_de_sessoes,
    inicializar,
    obter_sessao,
)

__all__ = [
    "CONVENCAO_DE_NOMES",
    "SCHEMAS",
    "IdMixin",
    "TimestampMixin",
    "criar_base",
    "criar_engine",
    "encerrar",
    "engine",
    "fabrica_de_sessoes",
    "inicializar",
    "obter_sessao",
]
