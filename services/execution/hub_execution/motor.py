"""O motor de execução: do pedido ao desfecho.

## O ciclo

    disparar()        -> grava Execucao(enfileirada) + publica na fila
    marcar_iniciada() <- runner avisa que começou
    registrar_logs()  <- runner envia log conforme sai
    finalizar()       <- runner envia status, métricas e artefatos

As três últimas são chamadas pelo `runner-service` via callback autenticado com
token de serviço, nunca com token de pessoa, ver
`hub_core.auth.dependencias.chamador_interno`. Alguém com token de usuário
válido não deve poder declarar que uma execução terminou com sucesso.

## Controle de admissão e a corrida que ele precisa evitar

Cada automação declara `max_execucoes_concorrentes`. Verificar o limite é
contar as execuções ativas dela e comparar. A implementação ingênua tem uma
janela de corrida clássica:

    T1: conta 1 ativa, limite 2  -> pode
    T2: conta 1 ativa, limite 2  -> pode     (T1 ainda não inseriu)
    T1: insere                               (2 ativas)
    T2: insere                               (3 ativas, limite furado)

Com uma réplica do serviço, um `SELECT ... FOR UPDATE` resolveria. Com várias,
não há linha comum para travar, a contagem é sobre linhas que ainda não
existem. É o caso de uso exato de um lock distribuído: `contar` e `inserir`
acontecem dentro de `lock.se_obtido(f"admissao:{slug}")`, serializando apenas
os disparos **da mesma automação**. Disparos de automações diferentes não se
esperam.

É por isso que `hub_core.locks` existe, e por isso o adaptador PostgreSQL
(advisory lock) é suficiente: o lock é curto, e se o processo morrer segurando
ele é liberado na hora pela queda da conexão.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from hub_core import auditoria, events
from hub_core.auth.rbac import Permissao
from hub_core.auth.tokens import Identidade
from hub_core.broker import Broker
from hub_core.errors import (
    Conflito,
    DependenciaIndisponivel,
    LimiteDeConcorrencia,
    NaoEncontrado,
    SemPermissao,
)
from hub_core.executor.base import StatusExecucao
from hub_core.locks import Lock
from hub_core.logging import obter_logger
from hub_core.storage import ArtefatoStore
from hub_execution.esquemas import (
    ExecucaoCartao,
    ExecucaoDetalhe,
    LinhaDeLog,
    PedidoDeDisparo,
    PedidoDeFinalizacao,
    ResumoDeArtefato,
)
from hub_execution.modelos import (
    Execucao,
    ExecucaoArtefato,
    ExecucaoLog,
    ExecucaoMetrica,
)

log = obter_logger(__name__)

SERVICO = "execution"

STATUS_ATIVOS = (StatusExecucao.ENFILEIRADA.value, StatusExecucao.EXECUTANDO.value)

PRIORIDADE_POR_FILA = {
    "execucoes.interativa": 8,
    "execucoes.padrao": 5,
    "execucoes.lote": 1,
}


# ─────────────────────────────────────────────────────────────────────────────
# Disparo
# ─────────────────────────────────────────────────────────────────────────────


async def disparar(
    sessao: AsyncSession,
    *,
    slug: str,
    quem: Identidade,
    pedido: PedidoDeDisparo,
    cliente_catalog: Any,
    broker: Broker,
    lock: Lock,
    token: str,
    origem: str = "manual",
    requisicao: Any = None,
) -> ExecucaoDetalhe:
    """Valida, admite e enfileira uma execução."""
    if not quem.pode(Permissao.EXECUCAO_DISPARAR):
        raise SemPermissao("seu papel não permite disparar execuções")

    # O catálogo decide o acesso e valida os parâmetros em uma chamada. O token
    # repassado é o da pessoa: a decisão de acesso tem de ser sobre quem
    # realmente está disparando.
    preparo = await cliente_catalog.post(
        f"/automacoes/{slug}/preparar-execucao",
        token=token,
        json={"parametros": pedido.parametros, "fila": pedido.fila},
    )

    if not preparo.get("permitido"):
        raise SemPermissao(
            preparo.get("motivo") or "você não pode disparar esta automação", slug=slug
        )

    fila = preparo.get("fila_sugerida") or "execucoes.padrao"
    limite = int(preparo.get("max_execucoes_concorrentes") or 1)

    nome_do_lock = f"admissao:{slug}"
    async with lock.se_obtido(nome_do_lock, ttl_segundos=20) as obtido:
        if not obtido:
            # Outro disparo da mesma automação está no meio da admissão. Pedir
            # para tentar de novo é melhor do que esperar: a espera seguraria
            # um worker web, e um clique duplo no botão é a causa mais
            # provável de dois disparos simultâneos.
            raise Conflito(
                "outro disparo desta automação está sendo processado",
                dica="tente novamente em alguns segundos",
            )

        ativas = await contar_ativas(sessao, slug)
        if ativas >= limite:
            raise LimiteDeConcorrencia(
                f"a automação já está com {ativas} de {limite} execuções simultâneas",
                ativas=ativas,
                limite=limite,
                dica="aguarde o término de uma execução",
            )

        execucao = Execucao(
            automacao_id=uuid.UUID(preparo["automacao_id"]),
            automacao_versao_id=(
                uuid.UUID(preparo["versao_id"]) if preparo.get("versao_id") else None
            ),
            automacao_slug=slug,
            automacao_nome=preparo.get("nome") or slug,
            automacao_versao=preparo.get("versao"),
            usuario_id=quem.id,
            usuario_nome=quem.nome,
            usuario_cpf=quem.cpf or None,
            unidade_id=quem.unidade_id,
            status=StatusExecucao.ENFILEIRADA.value,
            origem=origem,
            fila=fila,
            prioridade=PRIORIDADE_POR_FILA.get(fila, 5),
            parametros=preparo.get("parametros_saneados") or {},
        )
        sessao.add(execucao)
        await sessao.flush()

        await auditoria.registrar(
            sessao,
            acao=events.EXECUCAO_ENFILEIRADA,
            recurso=str(execucao.id),
            ator=quem,
            servico=SERVICO,
            requisicao=requisicao,
            detalhes={
                "automacao": slug,
                "origem": origem,
                "fila": fila,
                "parametros": execucao.parametros,
            },
        )
        await sessao.commit()

    # A publicação na fila acontece **depois** do commit, e isso é uma escolha
    # com consequência que vale nomear. Publicar dentro da transação criaria o
    # caso oposto e pior: o runner consome a mensagem, procura a execução no
    # banco e não a encontra, porque o commit ainda não aconteceu.
    #
    # Assim, a falha possível é uma execução gravada como `enfileirada` sem
    # mensagem na fila, visível, diagnosticável, e recuperável pelo
    # `recuperar_orfas` abaixo. A solução definitiva é o padrão *outbox*
    # transacional; está em docs/adr/0010 como evolução, não implementado.
    try:
        mensagem_id = await broker.publicar(
            fila,
            _corpo_da_mensagem(execucao, preparo),
            prioridade=execucao.prioridade,
        )
    except Exception as erro:
        execucao.status = StatusExecucao.FALHA.value
        execucao.erro = f"não foi possível enfileirar: {erro}"
        execucao.finalizada_em = datetime.now(UTC)
        await sessao.commit()
        raise DependenciaIndisponivel(
            "a fila de execuções não está disponível", execucao_id=str(execucao.id)
        ) from erro

    execucao.mensagem_id = mensagem_id
    await sessao.commit()

    log.info(
        "execucao.enfileirada",
        execucao_id=str(execucao.id),
        automacao=slug,
        fila=fila,
        origem=origem,
    )
    return await obter(sessao, execucao.id, quem)


def _corpo_da_mensagem(execucao: Execucao, preparo: dict[str, Any]) -> dict[str, Any]:
    """Monta o que o runner recebe.

    A mensagem é **autossuficiente**: carrega imagem, entrypoint, limites e
    roteiro, em vez de só o id da execução. O runner não precisa consultar o
    catálogo, e por consequência não precisa de credencial para isso.

    O custo é que a mensagem fica maior e congela a configuração no momento do
    disparo, se o limite de memória da automação mudar entre o enfileiramento
    e a execução, vale o antigo. Para uma fila que esvazia em segundos ou
    minutos, isso é aceitável; e, para uma execução reprocessada dias depois,
    é até preferível que ela rode com a configuração sob a qual foi aprovada.
    """
    return {
        "execucao_id": str(execucao.id),
        "automacao_slug": execucao.automacao_slug,
        "automacao_nome": execucao.automacao_nome,
        "imagem": preparo.get("imagem", ""),
        "entrypoint": preparo.get("entrypoint") or [],
        "parametros": execucao.parametros,
        "limites": preparo.get("limites") or {},
        "rede_habilitada": bool(preparo.get("rede_habilitada")),
        "variaveis_ambiente": preparo.get("variaveis_ambiente") or [],
        "roteiro_simulado": preparo.get("roteiro_simulado") or [],
    }


async def contar_ativas(sessao: AsyncSession, slug: str) -> int:
    resultado = await sessao.execute(
        select(func.count())
        .select_from(Execucao)
        .where(Execucao.automacao_slug == slug, Execucao.status.in_(STATUS_ATIVOS))
    )
    return int(resultado.scalar_one())


async def reexecutar(
    sessao: AsyncSession,
    execucao_id: uuid.UUID,
    *,
    quem: Identidade,
    cliente_catalog: Any,
    broker: Broker,
    lock: Lock,
    token: str,
    requisicao: Any = None,
) -> ExecucaoDetalhe:
    """Dispara de novo com os mesmos parâmetros.

    Não copia o resultado nem reusa a linha: cria uma execução nova, de origem
    `reexecucao`. Reaproveitar a linha apagaria o histórico da tentativa que
    falhou, que é justamente o que alguém vai querer comparar depois.

    Os parâmetros são repassados pelo caminho normal e **revalidados** pelo
    catálogo. Isso importa: a automação pode ter mudado de declaração desde a
    execução original, e reexecutar sem revalidar injetaria parâmetro que a
    versão atual não aceita mais.
    """
    original = await sessao.get(Execucao, execucao_id)
    if original is None:
        raise NaoEncontrado("execução não encontrada", execucao_id=str(execucao_id))

    if not quem.pode(Permissao.EXECUCAO_REEXECUTAR):
        raise SemPermissao("seu papel não permite reexecutar")

    if original.usuario_id != quem.id and not quem.pode(Permissao.EXECUCAO_VER_TODAS):
        raise SemPermissao("você só pode reexecutar as suas próprias execuções")

    return await disparar(
        sessao,
        slug=original.automacao_slug,
        quem=quem,
        pedido=PedidoDeDisparo(parametros=dict(original.parametros or {})),
        cliente_catalog=cliente_catalog,
        broker=broker,
        lock=lock,
        token=token,
        origem="reexecucao",
        requisicao=requisicao,
    )


async def cancelar(
    sessao: AsyncSession,
    execucao_id: uuid.UUID,
    *,
    quem: Identidade,
    cliente_runner: Any = None,
    requisicao: Any = None,
) -> ExecucaoDetalhe:
    execucao = await sessao.get(Execucao, execucao_id)
    if execucao is None:
        raise NaoEncontrado("execução não encontrada", execucao_id=str(execucao_id))

    if not quem.pode(Permissao.EXECUCAO_CANCELAR):
        raise SemPermissao("seu papel não permite cancelar execuções")

    if execucao.situacao.terminal:
        raise Conflito(
            f"a execução já está {execucao.situacao.rotulo.lower()}",
            status=execucao.status,
        )

    # Avisar o runner é melhor-esforço. Se ele não responder, a execução é
    # marcada como cancelada de todo jeito: deixá-la como "executando" por
    # causa de uma falha de rede faria o botão de cancelar parecer quebrado, e
    # a mensagem na fila é recolhida pelo `recuperar_expiradas` do broker.
    if cliente_runner is not None and execucao.situacao is StatusExecucao.EXECUTANDO:
        from hub_core.auth.tokens import emitir_token_de_servico
        from hub_core.settings import obter_settings

        try:
            await cliente_runner.post(
                f"/execucoes/{execucao_id}/cancelar",
                cabecalhos={
                    # Token de serviço, não o da pessoa: o runner precisa saber
                    # que o pedido vem do motor, que já registrou a decisão na
                    # auditoria.
                    "x-orbita-servico": emitir_token_de_servico("execution", obter_settings())
                },
            )
        except Exception:
            log.warning("execucao.cancelamento_nao_entregue", execucao_id=str(execucao_id))

    execucao.status = StatusExecucao.CANCELADA.value
    execucao.finalizada_em = datetime.now(UTC)
    execucao.resultado_resumo = f"Cancelada por {quem.nome}"
    _calcular_tempos(execucao)

    await _anexar_log(sessao, execucao_id, "WARNING", f"execução cancelada por {quem.nome}")
    await auditoria.registrar(
        sessao,
        acao=events.EXECUCAO_CANCELADA,
        recurso=str(execucao_id),
        ator=quem,
        servico=SERVICO,
        requisicao=requisicao,
        detalhes={"automacao": execucao.automacao_slug},
    )
    await sessao.commit()
    return await obter(sessao, execucao_id, quem)


# ─────────────────────────────────────────────────────────────────────────────
# Callbacks do runner
# ─────────────────────────────────────────────────────────────────────────────


async def marcar_iniciada(
    sessao: AsyncSession, execucao_id: uuid.UUID, *, executor: str, tentativa: int = 1
) -> None:
    execucao = await sessao.get(Execucao, execucao_id)
    if execucao is None:
        raise NaoEncontrado("execução não encontrada", execucao_id=str(execucao_id))

    if execucao.situacao.terminal:
        # A execução foi cancelada enquanto a mensagem estava em trânsito.
        # Não reabrir: o cancelamento é a palavra final.
        log.info("execucao.inicio_ignorado", execucao_id=str(execucao_id), status=execucao.status)
        return

    agora = datetime.now(UTC)
    execucao.status = StatusExecucao.EXECUTANDO.value
    execucao.iniciada_em = agora
    execucao.executor = executor
    execucao.tentativa = tentativa
    execucao.espera_ms = int((agora - execucao.enfileirada_em).total_seconds() * 1000)
    await sessao.commit()

    log.info("execucao.iniciada", execucao_id=str(execucao_id), executor=executor)


async def registrar_logs(
    sessao: AsyncSession, execucao_id: uuid.UUID, linhas: list[LinhaDeLog]
) -> int:
    """Insere um lote de linhas de log.

    Em lote, e não uma por chamada HTTP: uma automação verbosa emite centenas
    de linhas, e uma requisição por linha transformaria o log em gargalo. O
    runner acumula e envia em blocos.

    A `sequencia` é calculada a partir do máximo atual. Há aqui uma corrida
    teórica, dois lotes concorrentes lendo o mesmo máximo -, mas na prática
    não ocorre: uma execução tem exatamente um runner enviando logs. Se o
    desenho passasse a permitir vários produtores, isto precisaria de uma
    sequência do banco.
    """
    if not linhas:
        return 0

    atual = (
        await sessao.execute(
            select(func.coalesce(func.max(ExecucaoLog.sequencia), 0)).where(
                ExecucaoLog.execucao_id == execucao_id
            )
        )
    ).scalar_one()

    for deslocamento, linha in enumerate(linhas, start=1):
        sessao.add(
            ExecucaoLog(
                execucao_id=execucao_id,
                sequencia=atual + deslocamento,
                nivel=linha.nivel.upper()[:10],
                mensagem=linha.mensagem[:20_000],
                ocorrido_em=linha.ocorrido_em or datetime.now(UTC),
            )
        )

    await sessao.commit()
    return len(linhas)


async def finalizar(
    sessao: AsyncSession,
    execucao_id: uuid.UUID,
    pedido: PedidoDeFinalizacao,
    *,
    storage: ArtefatoStore,
) -> None:
    """Fecha a execução: status, resumo, métricas e artefatos."""
    execucao = await sessao.get(Execucao, execucao_id)
    if execucao is None:
        raise NaoEncontrado("execução não encontrada", execucao_id=str(execucao_id))

    if execucao.situacao is StatusExecucao.CANCELADA:
        log.info("execucao.finalizacao_ignorada", execucao_id=str(execucao_id))
        return

    execucao.status = pedido.status.value
    execucao.codigo_saida = pedido.codigo_saida
    execucao.resultado_resumo = (pedido.resumo or "")[:500] or None
    execucao.erro = pedido.erro
    execucao.consumo = pedido.consumo or {}
    execucao.finalizada_em = datetime.now(UTC)
    if execucao.iniciada_em is None:
        execucao.iniciada_em = execucao.finalizada_em
    _calcular_tempos(execucao)

    for nome, valor in (pedido.metricas or {}).items():
        sessao.add(
            ExecucaoMetrica(execucao_id=execucao_id, nome=str(nome)[:60], valor=float(valor))
        )

    for artefato in pedido.artefatos or []:
        sessao.add(
            ExecucaoArtefato(
                execucao_id=execucao_id,
                nome_arquivo=artefato.nome_arquivo[:260],
                caminho_storage=artefato.caminho_storage,
                tamanho_bytes=artefato.tamanho_bytes,
                tipo_conteudo=artefato.tipo_conteudo,
                sha256=artefato.sha256,
            )
        )

    await sessao.commit()
    del storage  # o upload é feito pelo runner; aqui só os metadados

    log.info(
        "execucao.finalizada",
        execucao_id=str(execucao_id),
        status=execucao.status,
        duracao_ms=execucao.duracao_ms,
        artefatos=len(pedido.artefatos or []),
    )

    await events.barramento.publicar(
        events.EventoDeDominio(
            nome=events.EXECUCAO_FINALIZADA,
            recurso=str(execucao_id),
            dados={
                "automacao": execucao.automacao_slug,
                "status": execucao.status,
                "duracao_ms": execucao.duracao_ms,
            },
            ator_id=execucao.usuario_id,
            ator_nome=execucao.usuario_nome,
        )
    )


def _calcular_tempos(execucao: Execucao) -> None:
    if execucao.iniciada_em and execucao.finalizada_em:
        execucao.duracao_ms = int(
            (execucao.finalizada_em - execucao.iniciada_em).total_seconds() * 1000
        )
    if execucao.enfileirada_em and execucao.iniciada_em and execucao.espera_ms is None:
        execucao.espera_ms = int(
            (execucao.iniciada_em - execucao.enfileirada_em).total_seconds() * 1000
        )


async def _anexar_log(
    sessao: AsyncSession, execucao_id: uuid.UUID, nivel: str, mensagem: str
) -> None:
    atual = (
        await sessao.execute(
            select(func.coalesce(func.max(ExecucaoLog.sequencia), 0)).where(
                ExecucaoLog.execucao_id == execucao_id
            )
        )
    ).scalar_one()
    sessao.add(
        ExecucaoLog(execucao_id=execucao_id, sequencia=atual + 1, nivel=nivel, mensagem=mensagem)
    )


# ─────────────────────────────────────────────────────────────────────────────
# Consultas
# ─────────────────────────────────────────────────────────────────────────────


def _para_cartao(execucao: Execucao) -> ExecucaoCartao:
    situacao = execucao.situacao
    return ExecucaoCartao(
        id=execucao.id,
        automacao_slug=execucao.automacao_slug,
        automacao_nome=execucao.automacao_nome,
        automacao_versao=execucao.automacao_versao,
        usuario_nome=execucao.usuario_nome,
        status=execucao.status,
        status_rotulo=situacao.rotulo,
        origem=execucao.origem,
        fila=execucao.fila,
        executor=execucao.executor,
        simulada=execucao.simulada,
        enfileirada_em=execucao.enfileirada_em,
        iniciada_em=execucao.iniciada_em,
        finalizada_em=execucao.finalizada_em,
        duracao_ms=execucao.duracao_ms,
        espera_ms=execucao.espera_ms,
        resultado_resumo=execucao.resultado_resumo,
        em_andamento=execucao.em_andamento,
    )


async def obter(
    sessao: AsyncSession,
    execucao_id: uuid.UUID,
    quem: Identidade,
    *,
    desde_sequencia: int = 0,
) -> ExecucaoDetalhe:
    """Detalhe da execução, com log a partir de `desde_sequencia`.

    O parâmetro `desde_sequencia` é o que torna o acompanhamento ao vivo viável
    com polling: a interface pede só o que chegou depois do que já tem, em vez
    de rebaixar o log inteiro a cada 2 segundos. Em uma execução com 2000
    linhas, a diferença é entre alguns bytes e alguns megabytes por intervalo.
    """
    consulta = (
        select(Execucao)
        .options(selectinload(Execucao.artefatos), selectinload(Execucao.metricas))
        .where(Execucao.id == execucao_id)
    )
    execucao = (await sessao.execute(consulta)).scalar_one_or_none()
    if execucao is None:
        raise NaoEncontrado("execução não encontrada", execucao_id=str(execucao_id))

    propria = execucao.usuario_id == quem.id
    if not propria and not quem.pode(Permissao.EXECUCAO_VER_TODAS):
        raise NaoEncontrado("execução não encontrada", execucao_id=str(execucao_id))

    logs = (
        (
            await sessao.execute(
                select(ExecucaoLog)
                .where(
                    ExecucaoLog.execucao_id == execucao_id,
                    ExecucaoLog.sequencia > desde_sequencia,
                )
                .order_by(ExecucaoLog.sequencia)
                .limit(2000)
            )
        )
        .scalars()
        .all()
    )

    cartao = _para_cartao(execucao)
    return ExecucaoDetalhe(
        **cartao.model_dump(),
        parametros=execucao.parametros or {},
        consumo=execucao.consumo or {},
        erro=execucao.erro,
        codigo_saida=execucao.codigo_saida,
        tentativa=execucao.tentativa,
        trace_id=execucao.trace_id,
        logs=[
            LinhaDeLog(
                sequencia=linha.sequencia,
                nivel=linha.nivel,
                mensagem=linha.mensagem,
                ocorrido_em=linha.ocorrido_em,
            )
            for linha in logs
        ],
        ultima_sequencia=(logs[-1].sequencia if logs else desde_sequencia),
        artefatos=[
            ResumoDeArtefato(
                id=a.id,
                nome_arquivo=a.nome_arquivo,
                caminho_storage=a.caminho_storage,
                tamanho_bytes=a.tamanho_bytes,
                tipo_conteudo=a.tipo_conteudo,
                sha256=a.sha256,
            )
            for a in execucao.artefatos
        ],
        metricas={m.nome: float(m.valor) for m in execucao.metricas},
    )


async def listar(
    sessao: AsyncSession,
    quem: Identidade,
    *,
    slug: str | None = None,
    status: str | None = None,
    origem: str | None = None,
    apenas_minhas: bool | None = None,
    pagina: int = 1,
    por_pagina: int = 25,
) -> tuple[list[ExecucaoCartao], int]:
    consulta = select(Execucao)

    # Quem não pode ver todas vê só as próprias, a restrição é aplicada aqui,
    # não deixada para quem chama. Filtro de autorização que depende de o
    # chamador lembrar de passar um parâmetro é filtro que um dia não é
    # passado.
    if not quem.pode(Permissao.EXECUCAO_VER_TODAS) or apenas_minhas:
        consulta = consulta.where(Execucao.usuario_id == quem.id)

    if slug:
        consulta = consulta.where(Execucao.automacao_slug == slug)
    if status:
        consulta = consulta.where(Execucao.status == status)
    if origem:
        consulta = consulta.where(Execucao.origem == origem)

    total = (
        await sessao.execute(select(func.count()).select_from(consulta.order_by(None).subquery()))
    ).scalar_one()

    pagina = max(1, pagina)
    por_pagina = min(max(1, por_pagina), 100)
    paginada = (
        consulta.order_by(Execucao.enfileirada_em.desc())
        .offset((pagina - 1) * por_pagina)
        .limit(por_pagina)
    )
    execucoes = (await sessao.execute(paginada)).scalars().all()
    return [_para_cartao(e) for e in execucoes], int(total)


async def recuperar_orfas(sessao: AsyncSession, *, broker: Broker) -> int:
    """Reenfileira execuções gravadas mas nunca publicadas na fila.

    É a rede de segurança da janela descrita em `disparar`: o commit da
    execução aconteceu e a publicação não. Uma execução `enfileirada` sem
    `mensagem_id` por mais de um minuto é, por definição, órfã, nenhum runner
    jamais vai pegá-la.

    Um minuto é folgado de propósito: é tempo mais que suficiente para a
    publicação normal concluir, e evita que esta rotina corra atrás de uma
    execução que está no caminho feliz.
    """
    from sqlalchemy import text as sql

    consulta = select(Execucao).where(
        Execucao.status == StatusExecucao.ENFILEIRADA.value,
        Execucao.mensagem_id.is_(None),
        Execucao.enfileirada_em < sql("now() - interval '1 minute'"),
    )
    orfas = (await sessao.execute(consulta)).scalars().all()

    recuperadas = 0
    for execucao in orfas:
        try:
            execucao.mensagem_id = await broker.publicar(
                execucao.fila,
                {
                    "execucao_id": str(execucao.id),
                    "automacao_slug": execucao.automacao_slug,
                    "automacao_nome": execucao.automacao_nome,
                    "parametros": execucao.parametros,
                    # Sem a configuração original: ela estava na mensagem
                    # perdida. O runner recarrega do catálogo neste caso.
                    "recuperada": True,
                },
                prioridade=execucao.prioridade,
            )
            recuperadas += 1
        except Exception:
            log.warning("execucao.recuperacao_falhou", execucao_id=str(execucao.id))
            break

    if recuperadas:
        await sessao.commit()
        log.warning("execucao.orfas_recuperadas", total=recuperadas)
    return recuperadas
