"""Fila de mensagens sobre uma tabela PostgreSQL.

É o adaptador padrão e o único que roda no free tier. Não é um atalho: uma fila
em `SELECT ... FOR UPDATE SKIP LOCKED` é um padrão estabelecido, usado em
produção por bibliotecas como `que`, `graphile-worker` e `River`. Vale até um
volume na casa de milhares de mensagens por minuto — ordens de magnitude acima
do que esta plataforma precisa.

**Por que `SKIP LOCKED` resolve.** O problema de uma fila em SQL é a corrida
entre consumidores: dois workers rodam o mesmo `SELECT`, pegam a mesma linha e
o trabalho roda duas vezes. `FOR UPDATE` sozinho não resolve — ele faz o
segundo worker *esperar* o primeiro, serializando os consumidores e matando o
paralelismo. `SKIP LOCKED` manda o segundo worker ignorar as linhas já
travadas e seguir para a próxima: cada um pega um lote distinto, sem espera e
sem sobreposição. Foi exatamente para isso que o PostgreSQL 9.5 o introduziu.

**Como o resto é obtido.** `visibility timeout` é a coluna `invisivel_ate` mais
um recuperador periódico; `retry com backoff` é `disponivel_em` no futuro; DLQ
é o estado `morta`. Nada disso exige um broker dedicado — exige uma transação e
um índice parcial.

O que se perde em relação ao RabbitMQ: consumo por *push* (aqui há *poll*, com
latência de até `FILA_INTERVALO_POLL_SEGUNDOS`), e a capacidade de absorver
picos de dezenas de milhares de mensagens por segundo. Ver docs/adr/0005.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hub_core.broker.base import Broker, Mensagem, backoff_segundos
from hub_core.logging import obter_logger, obter_trace_id

log = obter_logger(__name__)


_SQL_PUBLICAR = text(
    """
    INSERT INTO plataforma.fila_mensagens
        (fila, corpo, prioridade, max_tentativas, disponivel_em, trace_id)
    VALUES
        (:fila, :corpo, :prioridade, :max_tentativas,
         now() + make_interval(secs => :atraso), :trace_id)
    RETURNING id
    """
)

# A consulta que faz o trabalho. O CTE seleciona e trava as candidatas; o UPDATE
# externo as marca como entregues. Um comando só, uma transação só: não existe
# janela em que a mensagem esteja travada mas ainda visível a outro worker.
_SQL_CONSUMIR = text(
    """
    WITH proxima AS (
        SELECT id
          FROM plataforma.fila_mensagens
         WHERE fila = :fila
           AND estado = 'pendente'
           AND disponivel_em <= now()
         ORDER BY prioridade DESC, disponivel_em
         FOR UPDATE SKIP LOCKED
         LIMIT :limite
    )
    UPDATE plataforma.fila_mensagens AS m
       SET estado = 'processando',
           tentativas = m.tentativas + 1,
           invisivel_ate = now() + make_interval(secs => :visibilidade)
      FROM proxima
     WHERE m.id = proxima.id
    RETURNING m.id, m.fila, m.corpo, m.tentativas, m.criado_em, m.trace_id
    """
)

_SQL_CONFIRMAR = text(
    """
    UPDATE plataforma.fila_mensagens
       SET estado = 'concluida',
           invisivel_ate = NULL,
           finalizado_em = now()
     WHERE id = :id
       AND estado = 'processando'
    """
)

# Rejeição: ou volta para a fila com backoff, ou morre. A decisão é feita no
# banco, em um comando, para que duas rejeições concorrentes não cheguem a
# conclusões diferentes.
_SQL_REJEITAR = text(
    """
    UPDATE plataforma.fila_mensagens
       SET estado = CASE
                        WHEN tentativas >= max_tentativas THEN 'morta'
                        ELSE 'pendente'
                    END,
           disponivel_em = CASE
                        WHEN tentativas >= max_tentativas THEN disponivel_em
                        ELSE now() + make_interval(secs => :backoff)
                    END,
           invisivel_ate = NULL,
           ultimo_erro = :motivo,
           finalizado_em = CASE
                        WHEN tentativas >= max_tentativas THEN now()
                        ELSE NULL
                    END
     WHERE id = :id
    RETURNING estado, tentativas, max_tentativas
    """
)

# Rede de segurança para worker que morreu: o que foi entregue e nunca
# confirmado volta a ficar visível — ou vai para a DLQ se já gastou as
# tentativas.
_SQL_RECUPERAR = text(
    """
    UPDATE plataforma.fila_mensagens
       SET estado = CASE
                        WHEN tentativas >= max_tentativas THEN 'morta'
                        ELSE 'pendente'
                    END,
           invisivel_ate = NULL,
           disponivel_em = now(),
           ultimo_erro = COALESCE(
               ultimo_erro,
               'consumidor não confirmou antes do fim da visibilidade'
           ),
           finalizado_em = CASE
                        WHEN tentativas >= max_tentativas THEN now()
                        ELSE NULL
                    END
     WHERE estado = 'processando'
       AND invisivel_ate < now()
    RETURNING id, estado
    """
)

_SQL_PROFUNDIDADE = text(
    """
    SELECT estado, count(*) AS total
      FROM plataforma.fila_mensagens
     WHERE fila = :fila
     GROUP BY estado
    """
)

_SQL_LIMPAR = text(
    """
    DELETE FROM plataforma.fila_mensagens
     WHERE estado = 'concluida'
       AND finalizado_em < now() - make_interval(days => :dias)
    """
)

_SQL_LISTAR_MORTAS = text(
    """
    SELECT id, fila, corpo, tentativas, criado_em, ultimo_erro, finalizado_em
      FROM plataforma.fila_mensagens
     WHERE estado = 'morta'
     ORDER BY finalizado_em DESC NULLS LAST
     LIMIT :limite
    """
)

_SQL_REVIVER = text(
    """
    UPDATE plataforma.fila_mensagens
       SET estado = 'pendente',
           tentativas = 0,
           disponivel_em = now(),
           invisivel_ate = NULL,
           finalizado_em = NULL
     WHERE id = :id
       AND estado = 'morta'
    """
)


class BrokerPostgres(Broker):
    def __init__(
        self,
        fabrica: async_sessionmaker[AsyncSession],
        *,
        visibilidade_segundos: int = 300,
        max_tentativas: int = 3,
    ) -> None:
        self._fabrica = fabrica
        self._visibilidade = visibilidade_segundos
        self._max_tentativas = max_tentativas

    async def publicar(
        self,
        fila: str,
        corpo: dict[str, Any],
        *,
        atraso_segundos: int = 0,
        prioridade: int = 0,
        max_tentativas: int | None = None,
    ) -> uuid.UUID:
        import json

        async with self._fabrica() as sessao:
            resultado = await sessao.execute(
                _SQL_PUBLICAR,
                {
                    "fila": fila,
                    # O driver precisa de texto para o parâmetro JSONB; passar
                    # um dict faz o asyncpg tentar adaptá-lo e falhar.
                    "corpo": json.dumps(corpo, ensure_ascii=False, default=str),
                    "prioridade": prioridade,
                    "max_tentativas": max_tentativas or self._max_tentativas,
                    "atraso": float(atraso_segundos),
                    "trace_id": obter_trace_id(),
                },
            )
            msg_id = resultado.scalar_one()
            await sessao.commit()

        log.info(
            "fila.publicada",
            mensagem_id=str(msg_id),
            fila=fila,
            atraso=atraso_segundos,
            prioridade=prioridade,
        )
        return msg_id  # type: ignore[no-any-return]

    async def consumir(self, fila: str, *, limite: int = 1) -> list[Mensagem]:
        """Retira até `limite` mensagens. **Pode devolver menos — até zero.**

        Não é só o caso óbvio de fila vazia. Com `SKIP LOCKED`, o `LIMIT` é
        aplicado à varredura, não às linhas que o consumidor conseguiu travar:
        o PostgreSQL escolhe `limite` candidatas, descarta as que outro
        consumidor já travou, e devolve o que sobrou. Sob concorrência, pedir
        10 pode trazer 3.

        É comportamento correto e documentado, e é a razão de `consumir` não
        prometer um lote cheio. Quem chama precisa tratar o lote parcial — o
        `hub_runner.worker` usa `limite=1` e um laço, justamente para não
        depender disso.

        A invariante que **vale** é a que importa: uma mensagem nunca é
        entregue a dois consumidores.
        """
        async with self._fabrica() as sessao:
            resultado = await sessao.execute(
                _SQL_CONSUMIR,
                {"fila": fila, "limite": limite, "visibilidade": float(self._visibilidade)},
            )
            linhas = resultado.mappings().all()
            # O commit tem de sair antes de devolver as mensagens: enquanto a
            # transação está aberta, a marcação de "processando" não está
            # visível a outros workers, e um consumidor lento manteria as
            # linhas travadas sem necessidade.
            await sessao.commit()

        return [
            Mensagem(
                id=linha["id"],
                fila=linha["fila"],
                corpo=linha["corpo"],
                tentativas=linha["tentativas"],
                criado_em=linha["criado_em"],
                metadados={"trace_id": linha["trace_id"]},
            )
            for linha in linhas
        ]

    async def confirmar(self, mensagem: Mensagem) -> None:
        async with self._fabrica() as sessao:
            await sessao.execute(_SQL_CONFIRMAR, {"id": mensagem.id})
            await sessao.commit()
        log.info("fila.confirmada", mensagem_id=str(mensagem.id), fila=mensagem.fila)

    async def rejeitar(self, mensagem: Mensagem, *, motivo: str) -> None:
        espera = backoff_segundos(mensagem.tentativas)
        async with self._fabrica() as sessao:
            resultado = await sessao.execute(
                _SQL_REJEITAR,
                {"id": mensagem.id, "motivo": motivo[:2000], "backoff": float(espera)},
            )
            linha = resultado.mappings().first()
            await sessao.commit()

        if linha and linha["estado"] == "morta":
            log.warning(
                "fila.mensagem_morta",
                mensagem_id=str(mensagem.id),
                fila=mensagem.fila,
                tentativas=linha["tentativas"],
                motivo=motivo[:200],
            )
        else:
            log.info(
                "fila.reenfileirada",
                mensagem_id=str(mensagem.id),
                fila=mensagem.fila,
                proxima_tentativa_em_segundos=espera,
            )

    async def recuperar_expiradas(self) -> int:
        async with self._fabrica() as sessao:
            resultado = await sessao.execute(_SQL_RECUPERAR)
            linhas = resultado.mappings().all()
            await sessao.commit()

        if linhas:
            mortas = sum(1 for linha in linhas if linha["estado"] == "morta")
            log.warning(
                "fila.recuperadas",
                total=len(linhas),
                reenfileiradas=len(linhas) - mortas,
                mortas=mortas,
            )
        return len(linhas)

    async def profundidade(self, fila: str) -> dict[str, int]:
        async with self._fabrica() as sessao:
            resultado = await sessao.execute(_SQL_PROFUNDIDADE, {"fila": fila})
            contagem = {linha["estado"]: linha["total"] for linha in resultado.mappings()}

        return {
            estado: contagem.get(estado, 0)
            for estado in ("pendente", "processando", "concluida", "morta")
        }

    # ── Operações de manutenção, expostas na área administrativa ─────────────

    async def listar_mortas(self, *, limite: int = 50) -> list[dict[str, Any]]:
        async with self._fabrica() as sessao:
            resultado = await sessao.execute(_SQL_LISTAR_MORTAS, {"limite": limite})
            return [dict(linha) for linha in resultado.mappings()]

    async def reviver(self, mensagem_id: uuid.UUID) -> bool:
        """Devolve uma mensagem da DLQ para a fila, zerando as tentativas."""
        async with self._fabrica() as sessao:
            resultado = await sessao.execute(_SQL_REVIVER, {"id": mensagem_id})
            await sessao.commit()
        revivida = bool(resultado.rowcount)
        if revivida:
            log.info("fila.revivida", mensagem_id=str(mensagem_id))
        return revivida

    async def limpar_concluidas(self, *, dias: int = 30) -> int:
        """Retenção explícita. Chamado por um job, nunca pelo `ack`."""
        async with self._fabrica() as sessao:
            resultado = await sessao.execute(_SQL_LIMPAR, {"dias": dias})
            await sessao.commit()
        return resultado.rowcount or 0
