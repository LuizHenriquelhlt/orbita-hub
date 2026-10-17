"""Fila de mensagens sobre RabbitMQ.

Adaptador do ambiente que tem broker dedicado (docker-compose local,
Kubernetes). A interface é a mesma do adaptador PostgreSQL, então o
`runner-service` não sabe em qual dos dois está rodando.

Duas decisões de implementação valem explicação:

**Atraso sem plugin.** `atraso_segundos` existe para agendamento e para o
backoff do retry. O jeito usual é o plugin `rabbitmq_delayed_message_exchange`,
que não vem na imagem oficial, depender dele tornaria o `docker-compose up`
dependente de uma imagem customizada. Em vez disso, usamos o padrão clássico de
**filas de espera por faixa**: filas com `x-message-ttl` fixo e
`x-dead-letter-exchange` apontando de volta para a fila de trabalho. A mensagem
fica parada até o TTL vencer e é então "morta" para o destino real.

A sutileza: TTL *por mensagem* em uma fila compartilhada sofre bloqueio de
cabeça de fila, uma mensagem com TTL de 10 minutos na frente impede que as de
5 segundos atrás dela sejam encaminhadas, porque o RabbitMQ só examina a
cabeça. Faixas de TTL fixo eliminam o problema: dentro de cada fila, todas as
mensagens expiram na mesma ordem em que entraram.

**Visibility timeout é nativo.** No RabbitMQ, mensagem entregue e não confirmada
volta à fila quando o canal cai. Por isso `recuperar_expiradas` não faz nada
aqui, a garantia que o adaptador PostgreSQL precisa simular já existe.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from hub_core.broker.base import FILAS, Broker, Mensagem, backoff_segundos
from hub_core.logging import obter_logger, obter_trace_id

log = obter_logger(__name__)

EXCHANGE_TRABALHO = "orbita.trabalho"
EXCHANGE_ESPERA = "orbita.espera"
EXCHANGE_MORTAS = "orbita.mortas"
FILA_MORTAS = "orbita.mortas"

# Faixas de espera, em segundos. Cobrem o backoff exponencial de
# `hub_core.broker.base.backoff_segundos` (5, 10, 20, 40, ... com teto em 600).
FAIXAS_DE_ESPERA = (5, 10, 20, 40, 80, 160, 320, 600)


def _faixa_para(segundos: int) -> int:
    """Arredonda para cima até a faixa de espera mais próxima."""
    for faixa in FAIXAS_DE_ESPERA:
        if segundos <= faixa:
            return faixa
    return FAIXAS_DE_ESPERA[-1]


class BrokerRabbitMQ(Broker):
    def __init__(self, url: str, *, max_tentativas: int = 3, prefetch: int = 4) -> None:
        try:
            import aio_pika  # noqa: F401
        except ImportError as erro:  # pragma: no cover
            raise RuntimeError(
                "BROKER_BACKEND=rabbitmq exige o extra 'rabbitmq': "
                "pip install 'orbita-hub[rabbitmq]'"
            ) from erro

        self._url = url
        self._max_tentativas = max_tentativas
        self._prefetch = prefetch
        self._conexao: Any = None
        self._canal: Any = None
        self._exchange_trabalho: Any = None
        self._exchange_espera: Any = None
        self._filas: dict[str, Any] = {}
        # Mantém a entrega original viva até o ack/nack: `Mensagem` é um valor
        # imutável e não carrega o objeto do driver.
        self._entregas: dict[uuid.UUID, Any] = {}

    async def iniciar(self) -> None:
        import aio_pika

        self._conexao = await aio_pika.connect_robust(self._url)
        self._canal = await self._conexao.channel()
        await self._canal.set_qos(prefetch_count=self._prefetch)

        self._exchange_trabalho = await self._canal.declare_exchange(
            EXCHANGE_TRABALHO, aio_pika.ExchangeType.DIRECT, durable=True
        )
        self._exchange_espera = await self._canal.declare_exchange(
            EXCHANGE_ESPERA, aio_pika.ExchangeType.DIRECT, durable=True
        )
        exchange_mortas = await self._canal.declare_exchange(
            EXCHANGE_MORTAS, aio_pika.ExchangeType.FANOUT, durable=True
        )

        fila_mortas = await self._canal.declare_queue(FILA_MORTAS, durable=True)
        await fila_mortas.bind(exchange_mortas)

        for nome in FILAS:
            fila = await self._canal.declare_queue(
                nome,
                durable=True,
                arguments={
                    "x-max-priority": 10,
                    "x-dead-letter-exchange": EXCHANGE_MORTAS,
                },
            )
            await fila.bind(self._exchange_trabalho, routing_key=nome)
            self._filas[nome] = fila

            # Uma fila de espera por faixa e por fila de destino. O nome carrega
            # as duas informações porque o retorno é por `routing_key`.
            for faixa in FAIXAS_DE_ESPERA:
                espera = await self._canal.declare_queue(
                    f"{nome}.espera.{faixa}s",
                    durable=True,
                    arguments={
                        "x-message-ttl": faixa * 1000,
                        "x-dead-letter-exchange": EXCHANGE_TRABALHO,
                        "x-dead-letter-routing-key": nome,
                    },
                )
                await espera.bind(self._exchange_espera, routing_key=f"{nome}.{faixa}")

        log.info("fila.rabbitmq_pronta", filas=list(FILAS), faixas=list(FAIXAS_DE_ESPERA))

    async def encerrar(self) -> None:
        if self._conexao is not None:
            await self._conexao.close()
        self._conexao = None
        self._canal = None
        self._entregas.clear()

    def _exige_canal(self) -> None:
        if self._canal is None:
            raise RuntimeError("broker não iniciado, chame `await broker.iniciar()`")

    async def publicar(
        self,
        fila: str,
        corpo: dict[str, Any],
        *,
        atraso_segundos: int = 0,
        prioridade: int = 0,
        max_tentativas: int | None = None,
    ) -> uuid.UUID:
        import aio_pika

        self._exige_canal()
        mensagem_id = uuid.uuid4()

        envelope = aio_pika.Message(
            body=json.dumps(corpo, ensure_ascii=False, default=str).encode("utf-8"),
            content_type="application/json",
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            message_id=str(mensagem_id),
            priority=min(max(prioridade, 0), 10),
            timestamp=datetime.now(UTC),
            headers={
                "x-tentativas": 0,
                "x-max-tentativas": max_tentativas or self._max_tentativas,
                "x-fila-destino": fila,
                "x-trace-id": obter_trace_id() or "",
            },
        )

        if atraso_segundos > 0:
            faixa = _faixa_para(atraso_segundos)
            await self._exchange_espera.publish(envelope, routing_key=f"{fila}.{faixa}")
        else:
            await self._exchange_trabalho.publish(envelope, routing_key=fila)

        log.info(
            "fila.publicada",
            mensagem_id=str(mensagem_id),
            fila=fila,
            atraso=atraso_segundos,
            prioridade=prioridade,
        )
        return mensagem_id

    async def consumir(self, fila: str, *, limite: int = 1) -> list[Mensagem]:
        self._exige_canal()
        objeto_fila = self._filas.get(fila)
        if objeto_fila is None:
            raise ValueError(f"fila desconhecida: {fila!r}")

        colhidas: list[Mensagem] = []
        for _ in range(limite):
            entrega = await objeto_fila.get(no_ack=False, fail=False)
            if entrega is None:
                break

            cabecalhos = entrega.headers or {}
            tentativas = int(cabecalhos.get("x-tentativas", 0)) + 1
            mensagem_id = uuid.UUID(entrega.message_id) if entrega.message_id else uuid.uuid4()

            self._entregas[mensagem_id] = entrega
            colhidas.append(
                Mensagem(
                    id=mensagem_id,
                    fila=fila,
                    corpo=json.loads(entrega.body),
                    tentativas=tentativas,
                    criado_em=entrega.timestamp or datetime.now(UTC),
                    metadados={
                        "trace_id": cabecalhos.get("x-trace-id") or None,
                        "max_tentativas": int(
                            cabecalhos.get("x-max-tentativas", self._max_tentativas)
                        ),
                    },
                )
            )

        return colhidas

    async def confirmar(self, mensagem: Mensagem) -> None:
        entrega = self._entregas.pop(mensagem.id, None)
        if entrega is None:
            return
        await entrega.ack()
        log.info("fila.confirmada", mensagem_id=str(mensagem.id), fila=mensagem.fila)

    async def rejeitar(self, mensagem: Mensagem, *, motivo: str) -> None:
        import aio_pika

        entrega = self._entregas.pop(mensagem.id, None)
        if entrega is None:
            return

        max_tentativas = int(mensagem.metadados.get("max_tentativas", self._max_tentativas))

        if mensagem.tentativas >= max_tentativas:
            # `reject(requeue=False)` encaminha para o dead-letter-exchange
            # declarado na fila. É assim que a DLQ é alimentada.
            await entrega.reject(requeue=False)
            log.warning(
                "fila.mensagem_morta",
                mensagem_id=str(mensagem.id),
                fila=mensagem.fila,
                tentativas=mensagem.tentativas,
                motivo=motivo[:200],
            )
            return

        # Nova tentativa: confirma a entrega atual e republica na fila de
        # espera da faixa correspondente, com o contador incrementado.
        espera = backoff_segundos(mensagem.tentativas)
        faixa = _faixa_para(espera)

        cabecalhos = dict(entrega.headers or {})
        cabecalhos["x-tentativas"] = mensagem.tentativas
        cabecalhos["x-ultimo-erro"] = motivo[:500]

        await self._exchange_espera.publish(
            aio_pika.Message(
                body=entrega.body,
                content_type="application/json",
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                message_id=str(mensagem.id),
                priority=entrega.priority or 0,
                timestamp=entrega.timestamp or datetime.now(UTC),
                headers=cabecalhos,
            ),
            routing_key=f"{mensagem.fila}.{faixa}",
        )
        await entrega.ack()

        log.info(
            "fila.reenfileirada",
            mensagem_id=str(mensagem.id),
            fila=mensagem.fila,
            proxima_tentativa_em_segundos=faixa,
        )

    async def recuperar_expiradas(self) -> int:
        """No RabbitMQ a garantia é nativa: entrega não confirmada volta sozinha."""
        return 0

    async def profundidade(self, fila: str) -> dict[str, int]:
        self._exige_canal()
        objeto_fila = self._filas.get(fila)
        if objeto_fila is None:
            return {"pendente": 0, "processando": 0, "concluida": 0, "morta": 0}

        # `declare` passivo devolve as contagens atuais sem alterar a fila.
        declaracao = await self._canal.declare_queue(fila, passive=True)
        mortas = await self._canal.declare_queue(FILA_MORTAS, passive=True)
        return {
            "pendente": declaracao.declaration_result.message_count or 0,
            "processando": len(self._entregas),
            # O RabbitMQ não guarda histórico do que foi processado; esse número
            # vive na tabela de execuções, que é a fonte da verdade para
            # relatório. Aqui, só o que a fila sabe.
            "concluida": 0,
            "morta": mortas.declaration_result.message_count or 0,
        }
