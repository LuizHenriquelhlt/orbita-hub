"""Artefatos guardados no próprio PostgreSQL.

Adaptador do free tier, onde não há object storage. É uma troca consciente, com
limites explícitos:

* teto de tamanho por artefato (`ARTEFATO_TAMANHO_MAXIMO_BYTES`), porque o
  plano gratuito do Neon dá 0,5 GB para *tudo*, dado de domínio incluído;
* sem URL pré-assinada, o download passa pelo serviço;
* `bytea` é lido inteiro na memória do servidor antes de ser transmitido, então
  o `stream` daqui pagina em SQL (`substring`) em vez de pedir a coluna toda e
  fatiar em Python.

Esse último ponto é a diferença entre um adaptador que funciona na demonstração
e um que derruba um container de 512 MB: `SELECT conteudo FROM ...` com um
arquivo de 2 MB e dez downloads simultâneos já são 20 MB de buffer só aí.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hub_core.errors import EntradaInvalida, NaoEncontrado
from hub_core.logging import obter_logger
from hub_core.storage.base import Artefato, ArtefatoStore, validar_caminho

log = obter_logger(__name__)

_SQL_SALVAR = text(
    """
    INSERT INTO plataforma.artefato_blob
        (caminho, conteudo, tamanho_bytes, tipo_conteudo, sha256)
    VALUES
        (:caminho, :conteudo, :tamanho, :tipo, :sha256)
    ON CONFLICT (caminho) DO UPDATE
       SET conteudo = EXCLUDED.conteudo,
           tamanho_bytes = EXCLUDED.tamanho_bytes,
           tipo_conteudo = EXCLUDED.tipo_conteudo,
           sha256 = EXCLUDED.sha256,
           criado_em = now()
    RETURNING criado_em
    """
)

_SQL_METADADOS = text(
    """
    SELECT caminho, tamanho_bytes, tipo_conteudo, sha256, criado_em
      FROM plataforma.artefato_blob
     WHERE caminho = :caminho
    """
)

# `substring(bytea, inicio, tamanho)` é indexado em 1 e deixa a fatia a cargo
# do servidor: o Python nunca vê o arquivo inteiro.
_SQL_PEDACO = text(
    """
    SELECT substring(conteudo FROM :inicio FOR :tamanho) AS pedaco
      FROM plataforma.artefato_blob
     WHERE caminho = :caminho
    """
)

_SQL_CONTEUDO = text("SELECT conteudo FROM plataforma.artefato_blob WHERE caminho = :caminho")

_SQL_APAGAR = text("DELETE FROM plataforma.artefato_blob WHERE caminho = :caminho")

_SQL_CONTAR_DOWNLOAD = text(
    "UPDATE plataforma.artefato_blob SET downloads = downloads + 1 WHERE caminho = :caminho"
)


class ArtefatoStorePostgres(ArtefatoStore):
    def __init__(
        self,
        fabrica: async_sessionmaker[AsyncSession],
        *,
        tamanho_maximo_bytes: int = 2 * 1024 * 1024,
    ) -> None:
        self._fabrica = fabrica
        self._tamanho_maximo = tamanho_maximo_bytes

    async def salvar(
        self,
        caminho: str,
        conteudo: bytes,
        *,
        tipo_conteudo: str = "application/octet-stream",
    ) -> Artefato:
        caminho = validar_caminho(caminho)

        if len(conteudo) > self._tamanho_maximo:
            raise EntradaInvalida(
                "artefato excede o tamanho máximo deste ambiente",
                tamanho_bytes=len(conteudo),
                maximo_bytes=self._tamanho_maximo,
                dica="use STORAGE_BACKEND=s3 para arquivos grandes",
            )

        sha = hashlib.sha256(conteudo).hexdigest()
        async with self._fabrica() as sessao:
            resultado = await sessao.execute(
                _SQL_SALVAR,
                {
                    "caminho": caminho,
                    "conteudo": conteudo,
                    "tamanho": len(conteudo),
                    "tipo": tipo_conteudo,
                    "sha256": sha,
                },
            )
            criado_em = resultado.scalar_one()
            await sessao.commit()

        log.info("artefato.salvo", caminho=caminho, bytes=len(conteudo), backend="postgres")
        return Artefato(
            caminho=caminho,
            tamanho_bytes=len(conteudo),
            tipo_conteudo=tipo_conteudo,
            sha256=sha,
            criado_em=criado_em or datetime.now(UTC),
        )

    async def ler(self, caminho: str) -> bytes:
        caminho = validar_caminho(caminho)
        async with self._fabrica() as sessao:
            resultado = await sessao.execute(_SQL_CONTEUDO, {"caminho": caminho})
            linha = resultado.first()
        if linha is None:
            raise NaoEncontrado("artefato não encontrado", caminho=caminho)
        return bytes(linha[0])

    async def stream(self, caminho: str, *, pedaco_bytes: int = 64 * 1024) -> AsyncIterator[bytes]:
        caminho = validar_caminho(caminho)
        meta = await self.metadados(caminho)
        if meta is None:
            raise NaoEncontrado("artefato não encontrado", caminho=caminho)

        async with self._fabrica() as sessao:
            await sessao.execute(_SQL_CONTAR_DOWNLOAD, {"caminho": caminho})
            await sessao.commit()

        posicao = 0
        while posicao < meta.tamanho_bytes:
            async with self._fabrica() as sessao:
                resultado = await sessao.execute(
                    _SQL_PEDACO,
                    {"caminho": caminho, "inicio": posicao + 1, "tamanho": pedaco_bytes},
                )
                linha = resultado.first()
            if linha is None or not linha[0]:
                break
            pedaco = bytes(linha[0])
            yield pedaco
            posicao += len(pedaco)

    async def apagar(self, caminho: str) -> bool:
        caminho = validar_caminho(caminho)
        async with self._fabrica() as sessao:
            resultado = await sessao.execute(_SQL_APAGAR, {"caminho": caminho})
            await sessao.commit()
        return bool(resultado.rowcount)

    async def metadados(self, caminho: str) -> Artefato | None:
        caminho = validar_caminho(caminho)
        async with self._fabrica() as sessao:
            resultado = await sessao.execute(_SQL_METADADOS, {"caminho": caminho})
            linha = resultado.mappings().first()
        if linha is None:
            return None
        return Artefato(
            caminho=linha["caminho"],
            tamanho_bytes=linha["tamanho_bytes"],
            tipo_conteudo=linha["tipo_conteudo"],
            sha256=linha["sha256"],
            criado_em=linha["criado_em"],
        )
