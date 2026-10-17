"""Armazenamento de artefatos: porta, adaptadores e fábrica."""

from __future__ import annotations

from hub_core.settings import Settings
from hub_core.storage.base import Artefato, ArtefatoStore, validar_caminho
from hub_core.storage.postgres import ArtefatoStorePostgres

__all__ = [
    "Artefato",
    "ArtefatoStore",
    "ArtefatoStorePostgres",
    "construir_storage",
    "validar_caminho",
]


def construir_storage(settings: Settings) -> ArtefatoStore:
    """Escolhe o adaptador a partir de `STORAGE_BACKEND`."""
    if settings.storage_backend == "s3":
        from hub_core.storage.s3 import ArtefatoStoreS3

        return ArtefatoStoreS3(settings)

    from hub_core.db import fabrica_de_sessoes

    return ArtefatoStorePostgres(
        fabrica_de_sessoes(),
        tamanho_maximo_bytes=settings.artefato_tamanho_maximo_bytes,
    )
