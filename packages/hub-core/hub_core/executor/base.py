"""Porta do executor de automações, e o protocolo que automação e plataforma falam.

## O problema

Uma automação é código de terceiro. Pode estar em Python, Node, Go ou ser um
binário. Tem de poder emitir log, publicar métrica, declarar o arquivo que
produziu e dizer como terminou. A plataforma precisa receber isso enquanto a
automação roda, não no fim.

A solução óbvia é um SDK que faz chamadas HTTP de volta para o
`execution-service`. Foi a primeira escolhida e tem dois problemas: exige que
todo container tenha rota de rede até o serviço (e credencial para autenticar),
e exige um SDK por linguagem antes de a linguagem ser suportada.

## A solução adotada: linha de saída estruturada

A automação **imprime** no stdout. Linhas com o prefixo `@@orbita ` seguido de
um JSON são comandos de protocolo; qualquer outra linha é log em nível INFO.

    @@orbita {"tipo": "log", "nivel": "WARNING", "mensagem": "3 registros sem CNPJ"}
    @@orbita {"tipo": "metrica", "nome": "linhas_processadas", "valor": 1420}
    @@orbita {"tipo": "artefato", "caminho": "relatorio.xlsx"}
    @@orbita {"tipo": "resumo", "mensagem": "1420 outorgas em 8 regiões"}

Três consequências boas:

1. **Funciona nos três executores sem adaptação.** `docker logs`, o stdout de um
   subprocesso e a saída do simulador são a mesma coisa: linhas de texto.
2. **Não precisa de SDK.** Um `echo '@@orbita {"tipo":"log",...}'` em shell
   script é um cliente completo. O SDK em `sdk/hub_sdk` é conveniência tipada,
   não requisito.
3. **O container não precisa de rede nem de credencial.** Isso permite rodá-lo
   com `--network none`, que é o padrão para automação que só transforma
   arquivo.

O preço: o canal é unidirecional, então não há como a automação *consultar* a
plataforma durante a execução. Para o conjunto de automações batch que a
plataforma atende, nunca foi necessário. Ver docs/adr/0006.
"""

from __future__ import annotations

import abc
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

PREFIXO_PROTOCOLO = "@@orbita "

# Assinatura do callback que recebe log durante a execução.
#   (nivel, mensagem) -> None
AoRegistrarLog = Callable[[str, str], Awaitable[None]]


class StatusExecucao(StrEnum):
    ENFILEIRADA = "enfileirada"
    EXECUTANDO = "executando"
    SUCESSO = "sucesso"
    FALHA = "falha"
    CANCELADA = "cancelada"
    TIMEOUT = "timeout"

    @property
    def terminal(self) -> bool:
        return self in {
            StatusExecucao.SUCESSO,
            StatusExecucao.FALHA,
            StatusExecucao.CANCELADA,
            StatusExecucao.TIMEOUT,
        }

    @property
    def rotulo(self) -> str:
        return {
            StatusExecucao.ENFILEIRADA: "Na fila",
            StatusExecucao.EXECUTANDO: "Executando",
            StatusExecucao.SUCESSO: "Sucesso",
            StatusExecucao.FALHA: "Falha",
            StatusExecucao.CANCELADA: "Cancelada",
            StatusExecucao.TIMEOUT: "Tempo esgotado",
        }[self]


@dataclass(frozen=True, slots=True)
class LimitesDeRecurso:
    """Teto de consumo de uma execução.

    Respeitado integralmente pelo executor Docker (cgroups), parcialmente pelo
    de subprocesso (apenas tempo, via timeout) e ignorado pelo simulado. Quem
    lê o relatório precisa saber disso — por isso o status de uma execução
    guarda qual executor a rodou.
    """

    cpus: float = 0.5
    memoria_mb: int = 512
    timeout_segundos: int = 900
    disco_mb: int = 256


@dataclass(frozen=True, slots=True)
class PedidoDeExecucao:
    """Tudo que o executor precisa para rodar uma automação uma vez."""

    execucao_id: str
    automacao_slug: str
    imagem: str
    entrypoint: list[str] = field(default_factory=list)
    parametros: dict[str, Any] = field(default_factory=dict)
    variaveis_ambiente: dict[str, str] = field(default_factory=dict)
    limites: LimitesDeRecurso = field(default_factory=LimitesDeRecurso)
    rede_habilitada: bool = False
    """Se `False`, o container roda com `--network none`.

    O padrão é desligado: a maioria das automações só transforma arquivo, e uma
    automação sem rede não exfiltra nada nem é alcançada de fora. Quem precisa
    de rede declara no manifesto, e isso fica visível na ficha da automação.
    """
    roteiro_simulado: list[dict[str, Any]] = field(default_factory=list)
    """Passos que o `ExecutorSimulado` reproduz. Ver `executor/simulado.py`."""


@dataclass(slots=True)
class ArtefatoProduzido:
    caminho_relativo: str
    conteudo: bytes
    tipo_conteudo: str = "application/octet-stream"


@dataclass(slots=True)
class ResultadoExecucao:
    status: StatusExecucao
    codigo_saida: int | None = None
    resumo: str = ""
    metricas: dict[str, float] = field(default_factory=dict)
    artefatos: list[ArtefatoProduzido] = field(default_factory=list)
    erro: str | None = None
    consumo: dict[str, Any] = field(default_factory=dict)

    @property
    def sucesso(self) -> bool:
        return self.status is StatusExecucao.SUCESSO


@dataclass(slots=True)
class ComandoDeProtocolo:
    """Uma linha `@@orbita` já interpretada."""

    tipo: str
    dados: dict[str, Any]


def interpretar_linha(linha: str) -> ComandoDeProtocolo | None:
    """Traduz uma linha de saída em comando de protocolo, ou `None` se for log comum.

    Tolerante por desenho: uma linha que começa com o prefixo mas tem JSON
    inválido vira log, não erro. Uma automação não deve falhar porque emitiu
    uma linha de telemetria malformada — mas o problema precisa aparecer, então
    a linha crua é preservada no log.
    """
    if not linha.startswith(PREFIXO_PROTOCOLO):
        return None
    try:
        dados = json.loads(linha[len(PREFIXO_PROTOCOLO) :])
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(dados, dict) or "tipo" not in dados:
        return None
    return ComandoDeProtocolo(tipo=str(dados["tipo"]), dados=dados)


class Executor(abc.ABC):
    """Roda uma automação e devolve o que ela produziu."""

    nome: str = "abstrato"

    @abc.abstractmethod
    async def executar(
        self,
        pedido: PedidoDeExecucao,
        ao_registrar_log: AoRegistrarLog,
    ) -> ResultadoExecucao:
        """Executa de ponta a ponta.

        `ao_registrar_log` é chamado conforme a saída aparece, não no fim: é o
        que permite a interface mostrar o log de uma execução em andamento.

        Não deve levantar exceção por falha da automação — falha de automação é
        um `ResultadoExecucao` com status `FALHA`. Exceção aqui significa que o
        *executor* falhou, o que é outro problema e merece outra reação (a
        mensagem volta para a fila; a automação não é culpada).
        """

    async def cancelar(self, execucao_id: str) -> bool:
        """Interrompe uma execução em andamento. `False` se não for possível."""
        del execucao_id
        return False

    async def iniciar(self) -> None:
        return None

    async def encerrar(self) -> None:
        return None

    async def saudavel(self) -> tuple[bool, str]:
        """Diz se o executor consegue trabalhar, e por que não, se for o caso."""
        return True, "ok"
