"""Lock distribuído sobre *advisory locks* do PostgreSQL.

É o adaptador padrão, e o único que roda no free tier, onde não há Redis.

A semântica não é idêntica à do Redis, e vale entender a diferença antes de
confiar nela:

* **Redis** guarda uma chave com TTL. Se o processo que tomou o lock morrer, a
  chave só expira quando o TTL vencer; até lá ninguém mais entra.
* **PostgreSQL** amarra o lock à *sessão* (a conexão). Se o processo morrer, o
  servidor derruba a conexão e o lock é liberado **na hora**.

Para o caso de uso aqui, impedir que duas réplicas disparem o mesmo
agendamento, a liberação imediata é melhor: o trabalho é retomado em segundos
em vez de ficar parado esperando um TTL. O preço é que o parâmetro
`ttl_segundos` da interface não tem efeito neste adaptador; ele é aceito para
manter a assinatura única e é ignorado de forma explícita, não silenciosa.

O detalhe de implementação que importa: `pg_advisory_lock` é por sessão, então
cada lock precisa **manter a sua própria conexão aberta** enquanto o detém.
Pegar uma sessão do pool, dar o lock e devolver a conexão ao pool libera o lock
imediatamente, e o bug resultante é intermitente, porque depende de o pool
reaproveitar aquela conexão.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from hub_core.locks.base import Lock, chave_para_inteiro
from hub_core.logging import obter_logger

log = obter_logger(__name__)


class LockPostgres(Lock):
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._conexoes: dict[str, AsyncConnection] = {}

    async def adquirir(self, nome: str, *, ttl_segundos: int = 60) -> bool:
        del ttl_segundos  # ver docstring do módulo: o lock vive com a conexão

        if nome in self._conexoes:
            # Já detemos este lock neste processo. Não reentrante de propósito:
            # devolver True aqui faria o chamador liberar duas vezes.
            return False

        conexao = await self._engine.connect()
        try:
            resultado = await conexao.execute(
                text("SELECT pg_try_advisory_lock(:chave)"),
                {"chave": chave_para_inteiro(nome)},
            )
            obtido = bool(resultado.scalar())
        except Exception:
            await conexao.close()
            raise

        if not obtido:
            await conexao.close()
            return False

        self._conexoes[nome] = conexao
        log.debug("lock.obtido", nome=nome, backend="postgres")
        return True

    async def liberar(self, nome: str) -> None:
        conexao = self._conexoes.pop(nome, None)
        if conexao is None:
            return
        try:
            await conexao.execute(
                text("SELECT pg_advisory_unlock(:chave)"),
                {"chave": chave_para_inteiro(nome)},
            )
        except Exception:
            log.warning("lock.unlock_falhou", nome=nome, exc_info=True)
        finally:
            await conexao.close()
            log.debug("lock.liberado", nome=nome, backend="postgres")

    async def encerrar(self) -> None:
        for nome in list(self._conexoes):
            await self.liberar(nome)
