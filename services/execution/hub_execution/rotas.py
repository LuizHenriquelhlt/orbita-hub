"""Rotas HTTP do `execution-service`.

Dois grupos com regimes de autenticação diferentes:

* as rotas públicas da API, autenticadas com o token da pessoa;
* as rotas sob `/_internos`, autenticadas com **token de serviço**. São os
  callbacks por onde o `runner-service` reporta progresso. Alguém com token de
  usuário válido não deve poder declarar que uma execução terminou com
  sucesso, e é por isso que o segredo que as protege é outro
  (`INTERNAL_SHARED_SECRET`, não `JWT_SECRET`).
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from hub_core.auth import Identidade, Permissao, ServicoChamador, exigir
from hub_core.broker import FILAS
from hub_core.broker.postgres import BrokerPostgres
from hub_core.db import obter_sessao
from hub_core.errors import NaoEncontrado, SemPermissao
from hub_core.logging import obter_logger
from hub_execution import indicadores as ind
from hub_execution import motor
from hub_execution.esquemas import (
    ExecucaoDetalhe,
    Indicadores,
    PaginaDeExecucoes,
    PedidoDeDisparo,
    PedidoDeDisparoPorSlug,
    PedidoDeFinalizacao,
    PedidoDeInicio,
    PedidoDeLogs,
)
from hub_execution.modelos import ExecucaoArtefato

log = obter_logger(__name__)

Sessao = Annotated[AsyncSession, Depends(obter_sessao)]

router = APIRouter()
execucoes = APIRouter(prefix="/execucoes", tags=["execuções"])
painel = APIRouter(tags=["painel"])
filas = APIRouter(prefix="/filas", tags=["filas"])
internos = APIRouter(prefix="/_internos", include_in_schema=False)


def _token_de(request: Request) -> str:
    return request.headers.get("authorization", "").removeprefix("Bearer ").strip()


@execucoes.post("", response_model=ExecucaoDetalhe, status_code=202, summary="Disparar execução")
async def disparar(
    pedido: PedidoDeDisparoPorSlug,
    request: Request,
    sessao: Sessao,
    quem: Annotated[Identidade, Depends(exigir(Permissao.EXECUCAO_DISPARAR))],
) -> ExecucaoDetalhe:
    """202 e não 201: a execução foi **aceita**, não concluída.

    É a distinção que o código de status existe para fazer, e aqui ela é
    informativa: o cliente sabe que precisa acompanhar o resultado, em vez de
    assumir que o trabalho acabou quando a resposta chegou.
    """
    estado = request.app.state
    return await motor.disparar(
        sessao,
        slug=pedido.automacao,
        quem=quem,
        pedido=PedidoDeDisparo(parametros=pedido.parametros, fila=pedido.fila),
        cliente_catalog=estado.cliente_catalog,
        broker=estado.broker,
        lock=estado.lock,
        token=_token_de(request),
        requisicao=request,
    )


@execucoes.get("", response_model=PaginaDeExecucoes, summary="Listar execuções")
async def listar(
    sessao: Sessao,
    quem: Annotated[Identidade, Depends(exigir(Permissao.EXECUCAO_VER_PROPRIAS))],
    automacao: Annotated[str | None, Query(max_length=80)] = None,
    status: Annotated[str | None, Query(max_length=20)] = None,
    origem: Annotated[str | None, Query(max_length=20)] = None,
    apenas_minhas: bool = False,
    pagina: Annotated[int, Query(ge=1)] = 1,
    por_pagina: Annotated[int, Query(ge=1, le=100)] = 25,
) -> PaginaDeExecucoes:
    itens, total = await motor.listar(
        sessao,
        quem,
        slug=automacao,
        status=status,
        origem=origem,
        apenas_minhas=apenas_minhas,
        pagina=pagina,
        por_pagina=por_pagina,
    )
    return PaginaDeExecucoes(itens=itens, total=total, pagina=pagina, por_pagina=por_pagina)


@execucoes.get("/{execucao_id}", response_model=ExecucaoDetalhe, summary="Detalhe da execução")
async def detalhe(
    execucao_id: uuid.UUID,
    sessao: Sessao,
    quem: Annotated[Identidade, Depends(exigir(Permissao.EXECUCAO_VER_PROPRIAS))],
    desde_sequencia: Annotated[int, Query(ge=0)] = 0,
) -> ExecucaoDetalhe:
    """`desde_sequencia` é o que viabiliza o acompanhamento ao vivo por polling.

    A interface guarda a `ultima_sequencia` que recebeu e a devolve no próximo
    pedido, recebendo apenas as linhas novas. Em uma execução com 2000 linhas
    de log, é a diferença entre alguns bytes e alguns megabytes a cada
    intervalo de atualização.
    """
    return await motor.obter(sessao, execucao_id, quem, desde_sequencia=desde_sequencia)


@execucoes.post(
    "/{execucao_id}/cancelar", response_model=ExecucaoDetalhe, summary="Cancelar execução"
)
async def cancelar(
    execucao_id: uuid.UUID,
    request: Request,
    sessao: Sessao,
    quem: Annotated[Identidade, Depends(exigir(Permissao.EXECUCAO_CANCELAR))],
) -> ExecucaoDetalhe:
    return await motor.cancelar(
        sessao,
        execucao_id,
        quem=quem,
        cliente_runner=getattr(request.app.state, "cliente_runner", None),
        requisicao=request,
    )


@execucoes.post(
    "/{execucao_id}/reexecutar",
    response_model=ExecucaoDetalhe,
    status_code=202,
    summary="Disparar de novo com os mesmos parâmetros",
)
async def reexecutar(
    execucao_id: uuid.UUID,
    request: Request,
    sessao: Sessao,
    quem: Annotated[Identidade, Depends(exigir(Permissao.EXECUCAO_REEXECUTAR))],
) -> ExecucaoDetalhe:
    estado = request.app.state
    return await motor.reexecutar(
        sessao,
        execucao_id,
        quem=quem,
        cliente_catalog=estado.cliente_catalog,
        broker=estado.broker,
        lock=estado.lock,
        token=_token_de(request),
        requisicao=request,
    )


@execucoes.get("/{execucao_id}/artefatos/{artefato_id}", summary="Baixar um artefato")
async def baixar_artefato(
    execucao_id: uuid.UUID,
    artefato_id: uuid.UUID,
    request: Request,
    sessao: Sessao,
    quem: Annotated[Identidade, Depends(exigir(Permissao.EXECUCAO_VER_PROPRIAS))],
) -> Any:
    """Entrega o arquivo, por redirecionamento ou por streaming.

    Os dois caminhos existem porque os dois backends de armazenamento têm
    capacidades diferentes, e a porta `ArtefatoStore` admite isso em vez de
    fingir uniformidade (ver `hub_core.storage.base`):

    * **com S3/MinIO** há URL pré-assinada: responde 307 e o navegador baixa
      direto do object storage. Uma planilha de 50 MB não atravessa este
      processo;
    * **com artefato no PostgreSQL** não existe equivalente, então o arquivo é
      transmitido daqui, em pedaços paginados em SQL.

    A verificação de acesso acontece antes, nos dois casos, e é por isso que a
    URL pré-assinada é gerada aqui, com validade curta, em vez de guardada no
    banco junto do artefato.
    """
    # `motor.obter` já aplica a regra de visibilidade (própria execução ou
    # permissão de ver todas) e levanta 404 quando não se aplica.
    await motor.obter(sessao, execucao_id, quem)

    artefato = await sessao.get(ExecucaoArtefato, artefato_id)
    if artefato is None or artefato.execucao_id != execucao_id:
        raise NaoEncontrado("artefato não encontrado", artefato_id=str(artefato_id))

    storage = request.app.state.storage

    url = await storage.url_para_download(artefato.caminho_storage, validade_segundos=300)
    if url:
        return RedirectResponse(url, status_code=307)

    return StreamingResponse(
        storage.stream(artefato.caminho_storage),
        media_type=artefato.tipo_conteudo,
        headers={
            # `filename*` com UTF-8 para que nome de arquivo com acento chegue
            # correto; `filename` simples fica como alternativa para cliente
            # antigo.
            "content-disposition": (
                f'attachment; filename="{_ascii(artefato.nome_arquivo)}"; '
                f"filename*=UTF-8''{_percent(artefato.nome_arquivo)}"
            ),
            "content-length": str(artefato.tamanho_bytes),
            "x-orbita-sha256": artefato.sha256 or "",
        },
    )


def _ascii(nome: str) -> str:
    return nome.encode("ascii", errors="replace").decode("ascii").replace('"', "")


def _percent(nome: str) -> str:
    from urllib.parse import quote

    return quote(nome, safe="")


@painel.get("/indicadores", response_model=Indicadores, summary="Indicadores do painel")
async def indicadores(
    request: Request,
    sessao: Sessao,
    quem: Annotated[Identidade, Depends(exigir(Permissao.DASHBOARD_VER))],
) -> Indicadores:
    del quem
    return await ind.calcular(sessao, broker=getattr(request.app.state, "broker", None))


@painel.get("/automacoes/{slug}/estatisticas", summary="Estatísticas de uma automação")
async def estatisticas(
    slug: str,
    sessao: Sessao,
    quem: Annotated[Identidade, Depends(exigir(Permissao.EXECUCAO_VER_PROPRIAS))],
) -> dict[str, Any]:
    del quem
    return await ind.resumo_por_automacao(sessao, slug)


@filas.get("", summary="Profundidade das filas")
async def profundidade(
    request: Request,
    quem: Annotated[Identidade, Depends(exigir(Permissao.FILA_VER))],
) -> list[dict[str, Any]]:
    del quem
    broker = request.app.state.broker
    resultado = []
    for fila in FILAS:
        resultado.append({"fila": fila, **(await broker.profundidade(fila))})
    return resultado


@filas.get("/mortas", summary="Mensagens na fila de mensagens mortas")
async def mortas(
    request: Request,
    quem: Annotated[Identidade, Depends(exigir(Permissao.FILA_VER))],
    limite: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[dict[str, Any]]:
    del quem
    broker = request.app.state.broker
    if not isinstance(broker, BrokerPostgres):
        # A inspeção da DLQ é específica do adaptador PostgreSQL. No RabbitMQ,
        # a fila de mortas é inspecionada pelo painel de gestão dele, e
        # reimplementar isso aqui seria recriar uma ferramenta que já existe.
        raise SemPermissao(
            "a inspeção da DLQ pela API só existe com BROKER_BACKEND=postgres",
            dica="com RabbitMQ, use o painel de gestão em :15672",
        )
    return await broker.listar_mortas(limite=limite)


@filas.post("/mortas/{mensagem_id}/reviver", summary="Devolver mensagem morta para a fila")
async def reviver(
    mensagem_id: uuid.UUID,
    request: Request,
    sessao: Sessao,
    quem: Annotated[Identidade, Depends(exigir(Permissao.FILA_ADMINISTRAR))],
) -> dict[str, Any]:
    from hub_core import auditoria

    broker = request.app.state.broker
    if not isinstance(broker, BrokerPostgres):
        raise SemPermissao("disponível apenas com BROKER_BACKEND=postgres")

    revivida = await broker.reviver(mensagem_id)
    await auditoria.registrar(
        sessao,
        acao="fila.mensagem_revivida",
        recurso=str(mensagem_id),
        ator=quem,
        servico="execution",
        requisicao=request,
        detalhes={"revivida": revivida},
    )
    await sessao.commit()
    return {"revivida": revivida}


# ─────────────────────────────────────────────────────────────────────────────
# Callbacks do runner, token de serviço, não token de pessoa
# ─────────────────────────────────────────────────────────────────────────────


@internos.post("/execucoes/{execucao_id}/inicio", status_code=204)
async def callback_inicio(
    execucao_id: uuid.UUID,
    pedido: PedidoDeInicio,
    sessao: Sessao,
    chamador: ServicoChamador,
) -> None:
    log.debug("callback.inicio", execucao_id=str(execucao_id), de=chamador)
    await motor.marcar_iniciada(
        sessao, execucao_id, executor=pedido.executor, tentativa=pedido.tentativa
    )


@internos.post("/execucoes/{execucao_id}/logs")
async def callback_logs(
    execucao_id: uuid.UUID,
    pedido: PedidoDeLogs,
    sessao: Sessao,
    chamador: ServicoChamador,
) -> dict[str, int]:
    del chamador
    gravadas = await motor.registrar_logs(sessao, execucao_id, pedido.linhas)
    return {"gravadas": gravadas}


@internos.post("/execucoes/{execucao_id}/finalizacao", status_code=204)
async def callback_finalizacao(
    execucao_id: uuid.UUID,
    pedido: PedidoDeFinalizacao,
    request: Request,
    sessao: Sessao,
    chamador: ServicoChamador,
) -> None:
    log.debug("callback.finalizacao", execucao_id=str(execucao_id), de=chamador)
    await motor.finalizar(sessao, execucao_id, pedido, storage=request.app.state.storage)


_montado = False


def montar() -> APIRouter:
    global _montado
    if not _montado:
        for sub in (execucoes, painel, filas, internos):
            router.include_router(sub)
        _montado = True
    return router
