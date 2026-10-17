"""Porta da fila de mensagens.

A fila é o que desacopla "pedi uma execução" de "a execução rodou". O
`execution-service` publica; o `runner-service` consome. Nenhum dos dois sabe
qual tecnologia está no meio.

A interface foi desenhada a partir do que o RabbitMQ e uma tabela no PostgreSQL
**ambos** conseguem garantir, e não a partir do que o RabbitMQ oferece. Por
isso não há `exchange`, `routing key`, `fanout` nem consumo por *push*: são
conceitos que não têm equivalente honesto em SQL e, se estivessem aqui, o
adaptador PostgreSQL teria de simulá-los mal.

O que está garantido pelos dois adaptadores:

* entrega **ao menos uma vez** (o consumidor precisa ser idempotente);
* *visibility timeout*, mensagem entregue fica invisível por um prazo e volta
  à fila se não houver `confirmar` (ack) até lá;
* *retry* com backoff exponencial e um teto de tentativas;
* fila de mensagens mortas (DLQ) após o teto;
* prioridade entre mensagens da mesma fila.

O que **não** está garantido, e por isso não aparece na interface: ordenação
total, entrega exatamente-uma-vez e transações entre publicação e consumo.
"""

from __future__ import annotations

import abc
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# Filas da plataforma. Separadas por prioridade operacional, não por domínio:
# um relatório mensal pesado não deve atrasar a automação que alguém está
# olhando a tela esperando.
FILA_INTERATIVA = "execucoes.interativa"
FILA_PADRAO = "execucoes.padrao"
FILA_LOTE = "execucoes.lote"

FILAS = (FILA_INTERATIVA, FILA_PADRAO, FILA_LOTE)


@dataclass(frozen=True, slots=True)
class Mensagem:
    """Uma unidade de trabalho entregue a um consumidor."""

    id: uuid.UUID
    fila: str
    corpo: dict[str, Any]
    tentativas: int
    criado_em: datetime
    metadados: dict[str, Any] = field(default_factory=dict)

    @property
    def primeira_tentativa(self) -> bool:
        return self.tentativas <= 1


def backoff_segundos(tentativas: int, *, base: int = 5, teto: int = 600) -> int:
    """Backoff exponencial com teto: 5s, 10s, 20s, 40s... até 10 min.

    Sem o teto, a quinta tentativa de uma automação que falha por indisponibilidade
    de um sistema externo cairia em horas de espera, e a execução ficaria pendurada
    na interface sem explicação.
    """
    return min(base * (2 ** max(0, tentativas - 1)), teto)


class Broker(abc.ABC):
    """Fila de mensagens durável."""

    @abc.abstractmethod
    async def publicar(
        self,
        fila: str,
        corpo: dict[str, Any],
        *,
        atraso_segundos: int = 0,
        prioridade: int = 0,
        max_tentativas: int | None = None,
    ) -> uuid.UUID:
        """Enfileira uma mensagem e devolve o id dela.

        `atraso_segundos` é o que viabiliza agendamento e retry sem um
        componente separado: no RabbitMQ vira *delayed exchange*; no
        PostgreSQL, uma coluna `disponivel_em` no futuro.
        """

    @abc.abstractmethod
    async def consumir(self, fila: str, *, limite: int = 1) -> list[Mensagem]:
        """Retira até `limite` mensagens disponíveis, deixando-as invisíveis.

        Não bloqueia: devolve lista vazia se não houver nada. O laço de espera
        e o intervalo de *poll* ficam com o worker, que é quem sabe se deve
        desistir, dormir ou encerrar.
        """

    @abc.abstractmethod
    async def confirmar(self, mensagem: Mensagem) -> None:
        """Marca a mensagem como processada com sucesso (ack)."""

    @abc.abstractmethod
    async def rejeitar(self, mensagem: Mensagem, *, motivo: str) -> None:
        """Devolve a mensagem para nova tentativa, ou para a DLQ se esgotou (nack)."""

    @abc.abstractmethod
    async def recuperar_expiradas(self) -> int:
        """Devolve à fila o que foi entregue e nunca confirmado.

        É a rede de segurança para worker que morreu no meio do trabalho.
        Devolve quantas mensagens foram recuperadas.
        """

    @abc.abstractmethod
    async def profundidade(self, fila: str) -> dict[str, int]:
        """Contagem por estado, alimenta o dashboard e os alertas."""

    async def iniciar(self) -> None:
        return None

    async def encerrar(self) -> None:
        return None
