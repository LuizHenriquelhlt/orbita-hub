"""O agendador: dispara o que está vencido, uma vez só.

## Quem autoriza uma execução agendada

Uma execução disparada por cron não tem pessoa por trás. O agendador age como
uma **identidade de sistema** (`IDENTIDADE_DO_AGENDADOR`), com papel de
administrador, e isso precisa de justificativa porque parece um atalho.

Não é. A autorização de um agendamento acontece **no momento da publicação**,
não no do disparo: o gatilho cron é declarado no manifesto, e o manifesto passa
por homologação e aprovação de um gestor antes de a automação ser publicada.
Exigir, no disparo das 6h, que a identidade do agendador conste da lista de
acesso daquela automação seria verificar duas vezes a mesma decisão — e levaria
a conceder acesso explícito ao agendador em cada automação agendada, lista que
ninguém mantém.

O que mantém isso auditável: toda execução agendada é gravada com
`origem='agendada'` e `usuario_nome='Agendador'`, e a trilha de auditoria
registra qual gatilho a originou. Não há como uma execução agendada se passar
por execução de pessoa.

## Exclusão mútua

O `execution-service` pode ter várias réplicas, e todas rodam este laço. Sem
exclusão mútua, o agendamento das 6h dispararia uma vez por réplica. O laço
inteiro acontece dentro de `lock.se_obtido("agendador")` — se outra réplica já
está com o lock, esta simplesmente volta a dormir. É o caso de uso que
justifica `hub_core.locks`.

Há uma segunda proteção, independente do lock: `proxima_em` é atualizado na
mesma transação do disparo. Mesmo que duas réplicas passassem pelo lock (falha
de particionamento de rede, por exemplo), a segunda não encontraria o
agendamento vencido.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hub_core import auditoria
from hub_core.auth.rbac import Papel
from hub_core.auth.tokens import Identidade, emitir_token
from hub_core.broker import Broker
from hub_core.locks import Lock
from hub_core.logging import definir_trace_id, obter_logger
from hub_core.settings import Settings
from hub_execution import motor
from hub_execution.agenda import ExpressaoCronInvalida, interpretar
from hub_execution.esquemas import PedidoDeDisparo
from hub_execution.modelos import Agendamento

log = obter_logger(__name__)

# Id fixo para que a identidade do agendador seja reconhecível na trilha de
# auditoria entre reinícios e entre ambientes.
ID_DO_AGENDADOR = uuid.UUID("00000000-0000-4000-a000-000000000001")

IDENTIDADE_DO_AGENDADOR = Identidade(
    id=ID_DO_AGENDADOR,
    nome="Agendador",
    email="",
    cpf="",
    papeis=(Papel.ADMIN.value,),
)

INTERVALO_DO_LACO_SEGUNDOS = 30
INTERVALO_DE_SINCRONIZACAO_SEGUNDOS = 300


async def sincronizar(sessao: AsyncSession, *, cliente_catalog: Any, token: str) -> tuple[int, int]:
    """Alinha a tabela de agendamentos com os gatilhos cron do catálogo.

    O catálogo é a fonte da verdade sobre *quais agendamentos existem*; esta
    tabela guarda *o que já foi disparado*. Sincronizar é reconciliar as duas:
    gatilho novo entra, gatilho removido do manifesto sai.

    Devolve (criados_ou_atualizados, removidos).
    """
    try:
        gatilhos = await cliente_catalog.get("/automacoes/_internos/gatilhos-cron", token=token)
    except Exception as erro:
        log.warning("agendador.sincronizacao_falhou", erro=str(erro)[:200])
        return 0, 0

    vistos: set[uuid.UUID] = set()
    alterados = 0

    for bruto in gatilhos or []:
        expressao = bruto.get("expressao") or ""
        try:
            cron = interpretar(expressao)
        except ExpressaoCronInvalida as erro:
            # Expressão inválida chegou aqui apesar da validação do manifesto:
            # ou foi gravada antes de a validação existir, ou por outro
            # caminho. Registrar e ignorar é melhor do que derrubar o laço e
            # com ele todos os outros agendamentos.
            log.error(
                "agendador.expressao_invalida",
                slug=bruto.get("slug"),
                expressao=expressao,
                erro=str(erro),
            )
            continue

        gatilho_id = uuid.UUID(bruto["gatilho_id"])
        vistos.add(gatilho_id)

        agendamento = await sessao.get(Agendamento, gatilho_id)
        if agendamento is None:
            agendamento = Agendamento(
                gatilho_id=gatilho_id,
                automacao_slug=bruto["slug"],
                expressao=expressao,
                fila=bruto.get("fila") or "execucoes.lote",
                parametros_padrao=bruto.get("parametros_padrao") or {},
                proxima_em=cron.proxima_ocorrencia(),
            )
            sessao.add(agendamento)
            alterados += 1
        else:
            mudou = (
                agendamento.expressao != expressao
                or agendamento.fila != (bruto.get("fila") or agendamento.fila)
                or agendamento.automacao_slug != bruto["slug"]
            )
            agendamento.automacao_slug = bruto["slug"]
            agendamento.fila = bruto.get("fila") or agendamento.fila
            agendamento.parametros_padrao = bruto.get("parametros_padrao") or {}
            if mudou:
                agendamento.expressao = expressao
                # Expressão alterada invalida o `proxima_em` calculado com a
                # antiga; recalcular agora evita um disparo em horário errado.
                agendamento.proxima_em = cron.proxima_ocorrencia()
                alterados += 1
            elif agendamento.proxima_em is None:
                agendamento.proxima_em = cron.proxima_ocorrencia()

    removidos = 0
    if vistos:
        resultado = await sessao.execute(
            delete(Agendamento).where(Agendamento.gatilho_id.notin_(vistos))
        )
        removidos = resultado.rowcount or 0
    elif gatilhos is not None:
        # Lista vazia (e não erro) significa que não há mais nenhum gatilho
        # cron no catálogo. Limpar é o correto; o `elif` existe para não
        # apagar tudo quando a chamada falhou e `gatilhos` veio `None`.
        resultado = await sessao.execute(delete(Agendamento))
        removidos = resultado.rowcount or 0

    await sessao.commit()
    if alterados or removidos:
        log.info("agendador.sincronizado", alterados=alterados, removidos=removidos)
    return alterados, removidos


async def disparar_vencidos(
    sessao: AsyncSession,
    *,
    settings: Settings,
    cliente_catalog: Any,
    broker: Broker,
    lock: Lock,
    agora: datetime | None = None,
) -> int:
    """Dispara os agendamentos vencidos e reprograma cada um."""
    agora = agora or datetime.now(UTC)

    vencidos = (
        (
            await sessao.execute(
                select(Agendamento)
                .where(Agendamento.proxima_em.isnot(None), Agendamento.proxima_em <= agora)
                .order_by(Agendamento.proxima_em)
                .limit(20)
            )
        )
        .scalars()
        .all()
    )

    if not vencidos:
        return 0

    token, _ = emitir_token(IDENTIDADE_DO_AGENDADOR, settings)
    disparados = 0

    for agendamento in vencidos:
        definir_trace_id(f"cron-{agendamento.gatilho_id.hex[:12]}")
        try:
            cron = interpretar(agendamento.expressao)
        except ExpressaoCronInvalida:
            agendamento.proxima_em = None  # desarma; a sincronização avisa
            continue

        # Reprogramar **antes** de disparar. Se o disparo falhar, o
        # agendamento já está apontando para a próxima ocorrência e não
        # entra em laço tentando a mesma. Uma ocorrência perdida é melhor
        # do que um agendamento que dispara em loop.
        agendamento.proxima_em = cron.proxima_ocorrencia(depois_de=agora)
        agendamento.ultima_execucao_em = agora
        agendamento.disparos += 1

        try:
            execucao = await motor.disparar(
                sessao,
                slug=agendamento.automacao_slug,
                quem=IDENTIDADE_DO_AGENDADOR,
                pedido=PedidoDeDisparo(
                    parametros=dict(agendamento.parametros_padrao or {}),
                    fila=agendamento.fila,
                ),
                cliente_catalog=cliente_catalog,
                broker=broker,
                lock=lock,
                token=token,
                origem="agendada",
            )
            agendamento.ultima_execucao_id = execucao.id
            disparados += 1
            log.info(
                "agendador.disparou",
                slug=agendamento.automacao_slug,
                execucao_id=str(execucao.id),
                proxima_em=agendamento.proxima_em.isoformat() if agendamento.proxima_em else None,
            )
        except Exception as erro:
            # Limite de concorrência é o caso esperado: a execução anterior
            # ainda está rodando quando a próxima vence. Não é erro — é o
            # controle de admissão funcionando.
            log.warning(
                "agendador.disparo_recusado",
                slug=agendamento.automacao_slug,
                motivo=str(erro)[:200],
            )
            await auditoria.registrar(
                sessao,
                acao="agendamento.disparo_recusado",
                recurso=agendamento.automacao_slug,
                resultado=auditoria.FALHA,
                ator=IDENTIDADE_DO_AGENDADOR,
                servico="execution",
                detalhes={"motivo": str(erro)[:300]},
            )

        definir_trace_id(None)

    await sessao.commit()
    return disparados


async def laco(
    *,
    fabrica: async_sessionmaker[AsyncSession],
    settings: Settings,
    cliente_catalog: Any,
    broker: Broker,
    lock: Lock,
    parar: asyncio.Event,
) -> None:
    """O laço do agendador. Roda como tarefa de fundo do serviço.

    Encerra limpo quando `parar` é sinalizado — o que acontece no `shutdown`
    do serviço. Sem isso, o processo não termina e o orquestrador o mata por
    timeout, no meio de um possível disparo.
    """
    log.info("agendador.iniciado", intervalo=INTERVALO_DO_LACO_SEGUNDOS)
    ultima_sincronizacao = 0.0
    token, _ = emitir_token(IDENTIDADE_DO_AGENDADOR, settings)

    while not parar.is_set():
        try:
            async with lock.se_obtido("agendador", ttl_segundos=120) as obtido:
                if obtido:
                    agora = asyncio.get_running_loop().time()
                    async with fabrica() as sessao:
                        if agora - ultima_sincronizacao > INTERVALO_DE_SINCRONIZACAO_SEGUNDOS:
                            await sincronizar(sessao, cliente_catalog=cliente_catalog, token=token)
                            ultima_sincronizacao = agora

                        await disparar_vencidos(
                            sessao,
                            settings=settings,
                            cliente_catalog=cliente_catalog,
                            broker=broker,
                            lock=lock,
                        )
                        # Recolhe o que o broker entregou e nunca foi
                        # confirmado, e as execuções que ficaram sem mensagem.
                        await broker.recuperar_expiradas()
                        await motor.recuperar_orfas(sessao, broker=broker)
        except asyncio.CancelledError:
            raise
        except Exception:
            # O laço não pode morrer: se ele cair, nenhum agendamento dispara
            # mais e nada avisa. Registra e segue para a próxima volta.
            log.error("agendador.erro_no_laco", exc_info=True)

        try:
            await asyncio.wait_for(parar.wait(), timeout=INTERVALO_DO_LACO_SEGUNDOS)
        except TimeoutError:
            continue

    log.info("agendador.encerrado")
