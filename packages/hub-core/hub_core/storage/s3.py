"""Artefatos em object storage compatível com S3 (MinIO local, S3 em produção).

O `boto3` é síncrono e não tem versão assíncrona oficial. Chamá-lo direto de um
handler `async` bloquearia o event loop — e com um upload de alguns megabytes
isso significa parar de responder a *todas* as requisições do processo durante
a transferência. Cada chamada aqui vai para `asyncio.to_thread`.

A alternativa seria `aioboto3`, que embrulha o `aiobotocore`. Não foi adotada
porque acrescenta uma dependência que historicamente atrasa em relação ao
`botocore` e costuma travar a resolução de versões. Para o volume deste
projeto, um thread por operação de I/O é mais barato do que esse acoplamento.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from hub_core.errors import DependenciaIndisponivel, NaoEncontrado
from hub_core.logging import obter_logger
from hub_core.settings import Settings
from hub_core.storage.base import Artefato, ArtefatoStore, validar_caminho

log = obter_logger(__name__)


class ArtefatoStoreS3(ArtefatoStore):
    def __init__(self, settings: Settings) -> None:
        try:
            import boto3
            from botocore.config import Config
        except ImportError as erro:  # pragma: no cover
            raise RuntimeError(
                "STORAGE_BACKEND=s3 exige o extra 's3': pip install 'orbita-hub[s3]'"
            ) from erro

        self._bucket = settings.s3_bucket
        self._cliente: Any = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key,
            region_name=settings.s3_region,
            # `path` em vez de `virtual` porque o MinIO local não resolve
            # `bucket.host`; em S3 de verdade os dois funcionam.
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        )

    async def iniciar(self) -> None:
        """Garante que o bucket existe. Idempotente."""
        from botocore.exceptions import ClientError

        def _criar() -> None:
            try:
                self._cliente.head_bucket(Bucket=self._bucket)
            except ClientError as erro:
                codigo = erro.response.get("Error", {}).get("Code")
                if codigo in {"404", "NoSuchBucket"}:
                    self._cliente.create_bucket(Bucket=self._bucket)
                else:
                    raise

        try:
            await asyncio.to_thread(_criar)
        except Exception as erro:
            raise DependenciaIndisponivel(
                "não foi possível preparar o bucket de artefatos", bucket=self._bucket
            ) from erro
        log.info("artefato.bucket_pronto", bucket=self._bucket)

    async def salvar(
        self,
        caminho: str,
        conteudo: bytes,
        *,
        tipo_conteudo: str = "application/octet-stream",
    ) -> Artefato:
        caminho = validar_caminho(caminho)
        sha = hashlib.sha256(conteudo).hexdigest()

        await asyncio.to_thread(
            self._cliente.put_object,
            Bucket=self._bucket,
            Key=caminho,
            Body=conteudo,
            ContentType=tipo_conteudo,
            # Guardar o digest como metadado permite verificar integridade no
            # download sem reler o arquivo inteiro para recalcular.
            Metadata={"sha256": sha},
        )

        log.info("artefato.salvo", caminho=caminho, bytes=len(conteudo), backend="s3")
        return Artefato(
            caminho=caminho,
            tamanho_bytes=len(conteudo),
            tipo_conteudo=tipo_conteudo,
            sha256=sha,
            criado_em=datetime.now(UTC),
        )

    async def ler(self, caminho: str) -> bytes:
        from botocore.exceptions import ClientError

        caminho = validar_caminho(caminho)

        def _ler() -> bytes:
            resposta = self._cliente.get_object(Bucket=self._bucket, Key=caminho)
            return resposta["Body"].read()  # type: ignore[no-any-return]

        try:
            return await asyncio.to_thread(_ler)
        except ClientError as erro:
            if erro.response.get("Error", {}).get("Code") in {"NoSuchKey", "404"}:
                raise NaoEncontrado("artefato não encontrado", caminho=caminho) from erro
            raise

    async def stream(self, caminho: str, *, pedaco_bytes: int = 64 * 1024) -> AsyncIterator[bytes]:
        from botocore.exceptions import ClientError

        caminho = validar_caminho(caminho)

        try:
            resposta = await asyncio.to_thread(
                self._cliente.get_object, Bucket=self._bucket, Key=caminho
            )
        except ClientError as erro:
            if erro.response.get("Error", {}).get("Code") in {"NoSuchKey", "404"}:
                raise NaoEncontrado("artefato não encontrado", caminho=caminho) from erro
            raise

        corpo = resposta["Body"]
        while True:
            pedaco = await asyncio.to_thread(corpo.read, pedaco_bytes)
            if not pedaco:
                break
            yield pedaco

    async def apagar(self, caminho: str) -> bool:
        caminho = validar_caminho(caminho)
        if await self.metadados(caminho) is None:
            return False
        await asyncio.to_thread(self._cliente.delete_object, Bucket=self._bucket, Key=caminho)
        return True

    async def metadados(self, caminho: str) -> Artefato | None:
        from botocore.exceptions import ClientError

        caminho = validar_caminho(caminho)
        try:
            cabeca = await asyncio.to_thread(
                self._cliente.head_object, Bucket=self._bucket, Key=caminho
            )
        except ClientError:
            return None

        return Artefato(
            caminho=caminho,
            tamanho_bytes=cabeca.get("ContentLength", 0),
            tipo_conteudo=cabeca.get("ContentType", "application/octet-stream"),
            sha256=cabeca.get("Metadata", {}).get("sha256", ""),
            criado_em=cabeca.get("LastModified") or datetime.now(UTC),
        )

    async def url_para_download(self, caminho: str, *, validade_segundos: int = 900) -> str | None:
        """URL pré-assinada: o navegador baixa do object storage, não do serviço.

        É a vantagem real deste adaptador sobre o PostgreSQL — uma planilha de
        50 MB não atravessa o processo da aplicação.
        """
        caminho = validar_caminho(caminho)
        return await asyncio.to_thread(  # type: ignore[no-any-return]
            self._cliente.generate_presigned_url,
            "get_object",
            Params={"Bucket": self._bucket, "Key": caminho},
            ExpiresIn=validade_segundos,
        )
