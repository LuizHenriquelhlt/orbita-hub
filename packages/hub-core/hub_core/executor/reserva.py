"""Executor de reserva: cai no simulado quando a imagem não existe.

## O problema concreto

Com `EXECUTOR_BACKEND=docker`, toda automação precisa de uma imagem que o
Docker Engine consiga obter. No catálogo de exemplo, uma delas tem imagem
construída pelo próprio repositório (`make imagens`) e duas apontam para um
registry interno fictício, porque existem para demonstrar o mecanismo da
plataforma, não para rodar código.

Sem este envoltório, um `make up` limpo levava o visitante a clicar em
Executar e receber "não foi possível obter a imagem" em duas das três
automações. A plataforma estaria certa, e a demonstração, quebrada.

## O que ele faz

Tenta o executor principal. Se ele levantar `ImagemIndisponivel` **e** a
automação declarar um `roteiro_simulado` no manifesto, roda o roteiro pelo
`ExecutorSimulado` em vez de falhar.

Três cuidados mantêm isso honesto:

* **só `ImagemIndisponivel`.** Automação que roda e falha continua falhando;
  nada é mascarado. Qualquer outra exceção sobe, e o worker dá nack;
* **só com roteiro declarado.** Sem roteiro, não há o que simular, e a
  exceção sobe. Uma automação de produção cuja imagem sumiu **não** vira uma
  execução de mentira bem-sucedida;
* **o desvio aparece.** O log da execução recebe um aviso em nível WARNING, e
  o resultado é gravado com `executor='simulado'`, que a interface rotula.
  Quem olha a execução vê que ela não rodou em container.

## Quando desligar

`EXECUTOR_FALLBACK_SIMULADO=false`. Em um ambiente real é o que se quer: ali,
imagem ausente é um incidente, e mascará-lo com uma execução simulada seria
esconder exatamente o que precisa ser visto. O padrão é ligado apenas porque
o ambiente padrão deste repositório é o de demonstração.
"""

from __future__ import annotations

from hub_core.errors import ImagemIndisponivel
from hub_core.executor.base import (
    AoRegistrarLog,
    Executor,
    PedidoDeExecucao,
    ResultadoExecucao,
)
from hub_core.executor.simulado import ExecutorSimulado
from hub_core.logging import obter_logger

log = obter_logger(__name__)


class ExecutorComReserva(Executor):
    """Envolve um executor principal e desvia para o simulado quando cabível."""

    def __init__(self, principal: Executor, reserva: Executor | None = None) -> None:
        self._principal = principal
        self._reserva = reserva or ExecutorSimulado()

    @property
    def nome(self) -> str:  # type: ignore[override]
        # Reporta o nome do principal: é ele que roda no caminho normal, e é
        # esse nome que deve aparecer na tela de situação do runner. O desvio,
        # quando acontece, é registrado na própria execução.
        return self._principal.nome

    async def iniciar(self) -> None:
        await self._principal.iniciar()
        await self._reserva.iniciar()

    async def encerrar(self) -> None:
        await self._principal.encerrar()
        await self._reserva.encerrar()

    async def saudavel(self) -> tuple[bool, str]:
        ok, detalhe = await self._principal.saudavel()
        if ok:
            return True, f"{detalhe} (com reserva simulada)"
        # Principal indisponível não derruba o runner: ele ainda atende as
        # automações com roteiro. Dizer isso é melhor do que reportar o runner
        # como morto.
        return True, f"{detalhe} — atendendo apenas automações com roteiro"

    async def executar(
        self,
        pedido: PedidoDeExecucao,
        ao_registrar_log: AoRegistrarLog,
    ) -> ResultadoExecucao:
        try:
            return await self._principal.executar(pedido, ao_registrar_log)
        except ImagemIndisponivel as erro:
            if not pedido.roteiro_simulado:
                # Sem roteiro não há o que simular. A exceção sobe e o worker
                # devolve a mensagem para a fila, que é o tratamento correto
                # para uma imagem que pode voltar a existir.
                raise

            log.warning(
                "executor.desviou_para_reserva",
                execucao=pedido.execucao_id,
                automacao=pedido.automacao_slug,
                imagem=pedido.imagem,
                motivo=str(erro),
            )
            await ao_registrar_log(
                "WARNING",
                f"imagem {pedido.imagem} indisponível neste ambiente; "
                "executando o roteiro declarado no manifesto",
            )
            await ao_registrar_log(
                "WARNING",
                "esta execução NÃO rodou em container: o consumo é reproduzido, não medido",
            )
            return await self._reserva.executar(pedido, ao_registrar_log)

    async def cancelar(self, execucao_id: str) -> bool:
        # Tenta nos dois: a execução pode estar em qualquer um, e cancelar
        # onde ela não está é inofensivo.
        principal = await self._principal.cancelar(execucao_id)
        reserva = await self._reserva.cancelar(execucao_id)
        return principal or reserva

    def __getattr__(self, nome: str) -> object:
        """Repassa ao principal o que a porta não define.

        É o que mantém `listar_containers` funcionando na área de Runners: ele
        é específico do `ExecutorDocker` e não faz parte da interface
        `Executor`, então sem este repasse o envoltório o esconderia.
        """
        return getattr(self._principal, nome)
