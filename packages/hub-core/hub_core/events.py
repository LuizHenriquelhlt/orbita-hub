"""Barramento de eventos de domínio.

Serve a um propósito concreto e estreito: permitir que a auditoria registre o
que aconteceu sem que o código de negócio chame a auditoria. Quando o
`catalog-service` publica uma automação, ele emite `automacao.publicada` e
segue; quem quiser reagir, reage.

## Por que não é uma fila

O barramento é **em processo** e os assinantes rodam na mesma transação do
emissor. Isso é uma escolha, não uma limitação por falta de infraestrutura:

* o assinante que importa é a auditoria, e registro de auditoria que não é
  gravado junto com a ação auditada é registro que se perde exatamente quando
  mais importa — no erro;
* o número de assinantes é pequeno e conhecido;
* um barramento assíncrono entre serviços exigiria *outbox* transacional para
  não perder evento, e isso é complexidade que ainda não se paga.

Quando um assinante precisar de entrega assíncrona e durável, o caminho é ele
publicar na fila (`hub_core.broker`) de dentro do handler. O barramento não
tenta ser a fila.

## Erro em assinante não derruba o emissor

Um `except` por assinante, com log. O raciocínio: a ação de negócio já
aconteceu e é válida; falha ao *notificar* sobre ela não deve desfazê-la. A
exceção é a auditoria, que é registrada no mesmo `commit` do emissor por
escrever na sessão dele — ali, falha realmente desfaz tudo, e é o
comportamento desejado.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from hub_core.logging import obter_logger, obter_trace_id

log = obter_logger(__name__)

# Nomes de evento. Convenção `recurso.fato_no_passado`: o evento conta o que
# aconteceu, nunca o que deve acontecer. "automacao.publicar" seria um comando
# disfarçado, e comando tem destinatário, enquanto evento não.
AUTOMACAO_CRIADA = "automacao.criada"
AUTOMACAO_STATUS_ALTERADO = "automacao.status_alterado"
AUTOMACAO_VERSAO_PUBLICADA = "automacao.versao_publicada"

EXECUCAO_ENFILEIRADA = "execucao.enfileirada"
EXECUCAO_INICIADA = "execucao.iniciada"
EXECUCAO_FINALIZADA = "execucao.finalizada"
EXECUCAO_CANCELADA = "execucao.cancelada"

ACESSO_CONCEDIDO = "acesso.concedido"
LOGIN_REALIZADO = "login.realizado"
LOGIN_RECUSADO = "login.recusado"


@dataclass(frozen=True, slots=True)
class EventoDeDominio:
    nome: str
    recurso: str
    """Identificador do que foi afetado — slug da automação, id da execução."""
    dados: dict[str, Any] = field(default_factory=dict)
    ator_id: uuid.UUID | None = None
    ator_nome: str = ""
    ocorrido_em: datetime = field(default_factory=lambda: datetime.now(UTC))
    trace_id: str | None = field(default_factory=obter_trace_id)
    id: uuid.UUID = field(default_factory=uuid.uuid4)


Assinante = Callable[[EventoDeDominio], Awaitable[None]]


class BarramentoDeEventos:
    def __init__(self) -> None:
        self._assinantes: dict[str, list[Assinante]] = defaultdict(list)
        self._curinga: list[Assinante] = []

    def assinar(self, nome: str, assinante: Assinante) -> None:
        """Registra interesse em um evento. `"*"` assina todos."""
        if nome == "*":
            self._curinga.append(assinante)
        else:
            self._assinantes[nome].append(assinante)

    async def publicar(self, evento: EventoDeDominio) -> None:
        interessados = [*self._assinantes.get(evento.nome, ()), *self._curinga]

        log.info(
            "evento.publicado",
            evento=evento.nome,
            recurso=evento.recurso,
            assinantes=len(interessados),
        )

        for assinante in interessados:
            try:
                await assinante(evento)
            except Exception:
                log.error(
                    "evento.assinante_falhou",
                    evento=evento.nome,
                    assinante=getattr(assinante, "__qualname__", repr(assinante)),
                    exc_info=True,
                )

    def limpar(self) -> None:
        """Usado entre testes."""
        self._assinantes.clear()
        self._curinga.clear()


# Um barramento por processo. Em modo monolito, os quatro serviços o
# compartilham — o que é correto: um evento do catálogo precisa alcançar o
# assinante de auditoria, independentemente de quem o emitiu.
barramento = BarramentoDeEventos()
