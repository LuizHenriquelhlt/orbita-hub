"""Fábrica de aplicação FastAPI compartilhada pelos quatro serviços.

Cada serviço chama `criar_app(...)` e ganha, sem repetir código: tradução de
erro de domínio para HTTP, `trace_id` por requisição, endpoint de saúde,
`settings` no `app.state` e logging configurado.

A fábrica recebe o *router* e um ciclo de vida opcional, em vez de o serviço
construir o `FastAPI` e importar uma lista de middlewares. Assim, acrescentar
um middleware a toda a plataforma é uma mudança em um arquivo — e nenhum
serviço pode esquecer de aplicá-lo.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from hub_core import db
from hub_core.errors import ErroOrbita
from hub_core.logging import configurar_logging, definir_trace_id, obter_logger
from hub_core.settings import Settings, obter_settings

log = obter_logger(__name__)

CabecalhoTrace = "x-trace-id"

CicloDeVida = Callable[[FastAPI], Any]


def criar_app(
    *,
    nome: str,
    titulo: str,
    descricao: str = "",
    router: APIRouter | None = None,
    routers: list[APIRouter] | None = None,
    settings: Settings | None = None,
    ao_iniciar: CicloDeVida | None = None,
    ao_encerrar: CicloDeVida | None = None,
    usar_banco: bool = True,
) -> FastAPI:
    """Monta a aplicação de um serviço."""
    cfg = settings or obter_settings()
    configurar_logging(json=cfg.log_json, nivel=cfg.log_nivel)

    @asynccontextmanager
    async def ciclo_de_vida(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = cfg
        app.state.nome_servico = nome
        if usar_banco:
            db.inicializar(cfg)
        if ao_iniciar is not None:
            resultado = ao_iniciar(app)
            if hasattr(resultado, "__await__"):
                await resultado
        log.info("servico.iniciado", servico=nome, modo=cfg.hub_mode)
        try:
            yield
        finally:
            if ao_encerrar is not None:
                resultado = ao_encerrar(app)
                if hasattr(resultado, "__await__"):
                    await resultado
            log.info("servico.encerrado", servico=nome)

    app = FastAPI(
        title=titulo,
        description=descricao,
        version="0.1.0",
        lifespan=ciclo_de_vida,
        # Em modo monolito os quatro serviços viram um `/docs` só por serviço,
        # montados em prefixos distintos pelo gateway.
        docs_url="/docs",
        openapi_url="/openapi.json",
    )
    app.state.settings = cfg
    app.state.nome_servico = nome

    _registrar_middleware(app, nome)
    _registrar_tratadores_de_erro(app)
    _registrar_saude(app, nome, cfg)

    for r in [router, *(routers or [])]:
        if r is not None:
            app.include_router(r)

    return app


def _registrar_middleware(app: FastAPI, nome: str) -> None:
    @app.middleware("http")
    async def trace_e_tempo(request: Request, proxima: Any) -> Any:
        """Garante um `trace_id` por requisição e mede a duração.

        O id vem do cabeçalho se o chamador já tinha um — é assim que uma ação
        iniciada no portal mantém o mesmo id ao passar por `execution` e
        `runner`. Sem isso, cada serviço geraria o seu e a correlação se
        perderia justamente onde ela importa.
        """
        trace = request.headers.get(CabecalhoTrace) or uuid.uuid4().hex[:16]
        definir_trace_id(trace)
        comeco = time.perf_counter()

        resposta = await proxima(request)

        duracao_ms = (time.perf_counter() - comeco) * 1000
        resposta.headers[CabecalhoTrace] = trace
        resposta.headers["x-servico"] = nome
        resposta.headers["server-timing"] = f"app;dur={duracao_ms:.1f}"

        # Requisição lenta vira aviso; o resto fica em debug para não poluir.
        registrar = log.warning if duracao_ms > 2000 else log.debug
        registrar(
            "http.requisicao",
            metodo=request.method,
            caminho=request.url.path,
            status=resposta.status_code,
            duracao_ms=round(duracao_ms, 1),
        )
        return resposta


def _registrar_tratadores_de_erro(app: FastAPI) -> None:
    @app.exception_handler(ErroOrbita)
    async def erro_de_dominio(_request: Request, erro: ErroOrbita) -> JSONResponse:
        """Formato de erro único para toda a plataforma.

        O corpo carrega `codigo` além da mensagem, e é esse código que
        `ClienteServico._levantar` usa para reconstruir a exceção exata do
        outro lado — inclusive distinguindo dois erros que compartilham o mesmo
        status HTTP.
        """
        return JSONResponse(
            status_code=erro.status_http,
            content={
                "detail": {
                    "codigo": erro.codigo,
                    "mensagem": erro.mensagem,
                    "detalhes": {c: v for c, v in erro.detalhes.items() if _serializavel(v)},
                }
            },
        )

    @app.exception_handler(RequestValidationError)
    async def erro_de_validacao(_request: Request, erro: RequestValidationError) -> JSONResponse:
        """Traduz o erro do Pydantic para o mesmo formato, em português.

        O padrão do FastAPI devolve a estrutura crua do Pydantic, com `loc`,
        `ctx` e mensagens em inglês. Ela é ótima para depurar uma API e ruim
        para mostrar em formulário — e é o formulário do portal que consome
        isto.
        """
        campos = [
            {
                "campo": ".".join(str(parte) for parte in item.get("loc", ())[1:]) or "corpo",
                "problema": item.get("msg", ""),
            }
            for item in erro.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={
                "detail": {
                    "codigo": "entrada_invalida",
                    "mensagem": "Há campos inválidos no pedido.",
                    "detalhes": {"campos": campos},
                }
            },
        )

    @app.exception_handler(Exception)
    async def erro_inesperado(_request: Request, erro: Exception) -> JSONResponse:
        """Último recurso: registra com stack trace e responde sem vazar detalhe.

        A mensagem para o cliente é genérica de propósito. Caminho de arquivo,
        nome de tabela e fragmento de SQL em corpo de erro 500 são
        reconhecimento gratuito para quem está sondando o sistema; o
        `trace_id` na resposta é o que liga a reclamação do usuário ao log
        completo no servidor.
        """
        log.error("erro.inesperado", erro=str(erro), exc_info=True)
        return JSONResponse(
            status_code=500,
            content={
                "detail": {
                    "codigo": "erro_interno",
                    "mensagem": "Erro interno. O incidente foi registrado.",
                    "detalhes": {},
                }
            },
        )


def _registrar_saude(app: FastAPI, nome: str, cfg: Settings) -> None:
    @app.get("/saude", tags=["plataforma"], summary="Verificação de saúde")
    async def saude() -> dict[str, Any]:
        """Liveness + readiness em um endpoint.

        Deliberadamente **não** consulta o banco: este endpoint é chamado a
        cada poucos segundos pelo orquestrador, e um `SELECT 1` por chamada em
        um plano gratuito com limite de conexões é o tipo de verificação de
        saúde que causa a indisponibilidade que deveria detectar. A dependência
        real é verificada em `/saude/dependencias`, chamado sob demanda.
        """
        return {
            "servico": nome,
            "situacao": "ok",
            "modo": cfg.hub_mode,
            "ambiente": cfg.ambiente,
            "adaptadores": {
                "fila": cfg.broker_backend,
                "executor": cfg.executor_backend,
                "artefatos": cfg.storage_backend,
                "lock": cfg.lock_backend,
            },
        }

    @app.get("/saude/dependencias", tags=["plataforma"], summary="Checagem profunda")
    async def dependencias() -> dict[str, Any]:
        from sqlalchemy import text

        situacao: dict[str, Any] = {"banco": "indisponivel"}
        try:
            async with db.fabrica_de_sessoes()() as sessao:
                await sessao.execute(text("SELECT 1"))
            situacao["banco"] = "ok"
        except Exception as erro:
            situacao["banco_erro"] = str(erro)[:200]

        executor = getattr(app.state, "executor", None)
        if executor is not None:
            ok, detalhe = await executor.saudavel()
            situacao["executor"] = "ok" if ok else "indisponivel"
            situacao["executor_detalhe"] = detalhe

        return situacao


def _serializavel(valor: Any) -> bool:
    return isinstance(valor, str | int | float | bool | list | dict | type(None))
