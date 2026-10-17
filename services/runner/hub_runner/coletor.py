"""Coletor de logs: agrupa linhas antes de enviar ao `execution-service`.

## O problema que resolve

Uma automação de ETL emite facilmente 500 linhas de log. Uma requisição HTTP
por linha são 500 round-trips, 500 transações no banco e um log que chega mais
devagar do que é produzido. Mas acumular tudo e enviar no fim mata o
acompanhamento ao vivo, que é justamente o que faz alguém confiar que a
execução está progredindo.

A saída é agrupar por **tamanho ou tempo, o que vier primeiro**:

* 40 linhas acumuladas → envia (uma automação verbosa não atrasa);
* 1,5 s desde o último envio → envia (uma automação lenta não fica muda).

O resultado prático: uma execução que emite 500 linhas em 10 segundos gera
cerca de 13 requisições em vez de 500, e quem está olhando a tela vê o log
avançar a cada segundo e meio.

## Por que uma tarefa de fundo, e não `flush` no caminho do callback

Se o envio acontecesse dentro de `registrar`, a automação ficaria bloqueada
esperando o HTTP a cada 40 linhas — o log passaria a controlar a velocidade da
execução. Com uma tarefa separada drenando a fila, `registrar` só põe a linha
em memória e volta na hora.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from hub_core.logging import obter_logger

log = obter_logger(__name__)

LOTE_MAXIMO = 40
INTERVALO_MAXIMO_SEGUNDOS = 1.5
TETO_DA_FILA = 5000
"""Limite de linhas em memória por execução.

Uma automação em laço emitindo log sem parar consumiria a memória do runner.
Ao atingir o teto, as linhas novas são descartadas e uma única linha de aviso é
registrada — perder log é ruim, derrubar o runner e com ele todas as execuções
é pior.
"""


class ColetorDeLogs:
    """Acumula linhas de uma execução e as envia em lotes."""

    def __init__(self, cliente_execution: Any, execucao_id: str) -> None:
        self._cliente = cliente_execution
        self._execucao_id = execucao_id
        self._fila: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=TETO_DA_FILA)
        self._tarefa: asyncio.Task[None] | None = None
        self._encerrando = asyncio.Event()
        self._descartadas = 0
        self._avisou_descarte = False
        self.total_enviadas = 0

    async def __aenter__(self) -> ColetorDeLogs:
        self._tarefa = asyncio.create_task(self._drenar(), name=f"logs-{self._execucao_id[:8]}")
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.encerrar()

    async def registrar(self, nivel: str, mensagem: str) -> None:
        """Enfileira uma linha. Nunca bloqueia e nunca levanta exceção.

        É chamado de dentro do executor, no meio da execução da automação. Uma
        exceção aqui abortaria a automação por causa de uma falha de
        *telemetria*, o que seria trocar o problema maior pelo menor.
        """
        linha = {
            "nivel": (nivel or "INFO").upper()[:10],
            "mensagem": (mensagem or "")[:20_000],
            "ocorrido_em": datetime.now(UTC).isoformat(),
        }
        try:
            self._fila.put_nowait(linha)
        except asyncio.QueueFull:
            self._descartadas += 1
            if not self._avisou_descarte:
                self._avisou_descarte = True
                log.warning(
                    "coletor.fila_cheia",
                    execucao_id=self._execucao_id,
                    teto=TETO_DA_FILA,
                )

    async def _drenar(self) -> None:
        pendentes: list[dict[str, Any]] = []
        ultimo_envio = asyncio.get_running_loop().time()

        while True:
            tempo_restante = INTERVALO_MAXIMO_SEGUNDOS - (
                asyncio.get_running_loop().time() - ultimo_envio
            )
            try:
                linha = await asyncio.wait_for(self._fila.get(), timeout=max(0.05, tempo_restante))
                pendentes.append(linha)
            except TimeoutError:
                pass

            agora = asyncio.get_running_loop().time()
            lote_cheio = len(pendentes) >= LOTE_MAXIMO
            tempo_esgotado = agora - ultimo_envio >= INTERVALO_MAXIMO_SEGUNDOS

            if pendentes and (lote_cheio or tempo_esgotado):
                await self._enviar(pendentes)
                pendentes = []
                ultimo_envio = agora

            if self._encerrando.is_set() and self._fila.empty():
                if pendentes:
                    await self._enviar(pendentes)
                return

    async def _enviar(self, linhas: list[dict[str, Any]]) -> None:
        try:
            await self._cliente.post(
                f"/_internos/execucoes/{self._execucao_id}/logs",
                json={"linhas": linhas},
                cabecalhos=self._cliente_cabecalhos(),
            )
            self.total_enviadas += len(linhas)
        except Exception as erro:
            # Log perdido não deve derrubar a execução. Registrar localmente
            # garante que a informação não desaparece de todo — ela fica no
            # stdout do runner, que o coletor de logs da infraestrutura
            # recolhe.
            log.warning(
                "coletor.envio_falhou",
                execucao_id=self._execucao_id,
                linhas=len(linhas),
                erro=str(erro)[:200],
            )
            for linha in linhas:
                log.info(
                    "execucao.log_nao_entregue",
                    execucao_id=self._execucao_id,
                    nivel=linha["nivel"],
                    mensagem=linha["mensagem"][:500],
                )

    def _cliente_cabecalhos(self) -> dict[str, str]:
        from hub_core.auth.tokens import emitir_token_de_servico
        from hub_core.settings import obter_settings

        return {"x-orbita-servico": emitir_token_de_servico("runner", obter_settings())}

    async def encerrar(self) -> None:
        """Esvazia o que falta e encerra a tarefa de drenagem.

        O timeout existe porque o `execution-service` pode estar fora do ar: o
        runner não pode ficar preso tentando entregar log de uma execução que
        já terminou.
        """
        self._encerrando.set()
        if self._tarefa is not None:
            try:
                await asyncio.wait_for(self._tarefa, timeout=10)
            except TimeoutError:
                self._tarefa.cancel()
                log.warning("coletor.encerramento_forcado", execucao_id=self._execucao_id)

        if self._descartadas:
            log.warning(
                "coletor.linhas_descartadas",
                execucao_id=self._execucao_id,
                total=self._descartadas,
            )
