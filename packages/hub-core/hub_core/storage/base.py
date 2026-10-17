"""Porta do armazenamento de artefatos.

Artefato é o que uma automação produz e alguém vai baixar: a planilha do
relatório, o PDF do ofício, o CSV de erros. É dado de saída, não de domínio,
por isso não mora em tabela de negócio.

A interface tem uma assimetria deliberada: `url_para_download` pode devolver
`None`. Com S3/MinIO existe URL pré-assinada, e o navegador baixa direto do
object storage sem passar pela aplicação. Com artefato em PostgreSQL não existe
equivalente, o download tem de passar pelo serviço, que faz streaming do
`bytea`. Em vez de inventar uma URL falsa para uniformizar, a porta admite que
a capacidade não existe nos dois lados, e quem chama trata os dois casos. Uma
abstração que esconde a diferença faria o adaptador PostgreSQL servir URLs que
não funcionam.
"""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime

from hub_core.errors import EntradaInvalida


@dataclass(frozen=True, slots=True)
class Artefato:
    """Metadados de um arquivo guardado."""

    caminho: str
    tamanho_bytes: int
    tipo_conteudo: str
    sha256: str
    criado_em: datetime

    @property
    def nome_arquivo(self) -> str:
        return self.caminho.rsplit("/", 1)[-1]

    @property
    def tamanho_legivel(self) -> str:
        tamanho = float(self.tamanho_bytes)
        for unidade in ("B", "KB", "MB", "GB"):
            if tamanho < 1024 or unidade == "GB":
                return f"{tamanho:.0f} {unidade}" if unidade == "B" else f"{tamanho:.1f} {unidade}"
            tamanho /= 1024
        return f"{tamanho:.1f} GB"


class ArtefatoStore(abc.ABC):
    """Armazenamento de arquivos produzidos por execuções."""

    @abc.abstractmethod
    async def salvar(
        self,
        caminho: str,
        conteudo: bytes,
        *,
        tipo_conteudo: str = "application/octet-stream",
    ) -> Artefato:
        """Grava o conteúdo e devolve os metadados. Sobrescreve se já existir."""

    @abc.abstractmethod
    async def ler(self, caminho: str) -> bytes:
        """Devolve o conteúdo inteiro. Levanta `NaoEncontrado` se não existir."""

    @abc.abstractmethod
    async def stream(self, caminho: str, *, pedaco_bytes: int = 64 * 1024) -> AsyncIterator[bytes]:
        """Devolve o conteúdo em pedaços, para `StreamingResponse`."""

    @abc.abstractmethod
    async def apagar(self, caminho: str) -> bool:
        """Remove. Devolve `False` se não havia nada para remover."""

    @abc.abstractmethod
    async def metadados(self, caminho: str) -> Artefato | None:
        """Metadados sem ler o conteúdo."""

    async def url_para_download(self, caminho: str, *, validade_segundos: int = 900) -> str | None:
        """URL pré-assinada, quando o backend tiver uma. `None` caso contrário.

        Quem chama deve tratar o `None` servindo o arquivo pelo próprio
        serviço, via `stream`.
        """
        del caminho, validade_segundos
        return None

    async def iniciar(self) -> None:
        return None

    async def encerrar(self) -> None:
        return None


def validar_caminho(caminho: str) -> str:
    """Rejeita caminho que escaparia do prefixo da execução.

    Os artefatos são nomeados pela própria automação, que é código de terceiro
    rodando dentro da plataforma. `../../etc/passwd` como nome de artefato é a
    primeira coisa que se tenta, e no adaptador S3 um caminho assim de fato
    escreve fora do prefixo esperado.
    """
    limpo = caminho.strip().lstrip("/")
    if not limpo:
        raise EntradaInvalida("caminho de artefato vazio")
    if ".." in limpo.split("/") or "\\" in limpo:
        raise EntradaInvalida("caminho de artefato inválido", caminho=caminho)
    if len(limpo) > 500:
        raise EntradaInvalida("caminho de artefato muito longo", tamanho=len(limpo))
    return limpo
