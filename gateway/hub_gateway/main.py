"""O gateway: compõe os quatro serviços em um processo só.

    HUB_MODE=monolith uvicorn hub_gateway.main:app --port 8000

É o artefato que roda no free tier. Monta as quatro aplicações FastAPI sob
prefixos, registra cada uma como alvo in-process para os clientes
(`hub_core.clients`), e sobe o portal Django na raiz.

    /api/identity/...   -> hub_identity.app
    /api/catalog/...    -> hub_catalog.app
    /api/execution/...  -> hub_execution.app
    /api/runner/...     -> hub_runner.app
    /...                -> portal Django (BFF)

## A armadilha do ASGITransport, e como ela é resolvida aqui

`httpx.ASGITransport` chama a aplicação com escopo de **requisição**. Ele nunca
envia o escopo de `lifespan`. Então os eventos de inicialização dos serviços
embutidos, criar o engine do banco, abrir o broker, instanciar o executor,
simplesmente não rodam, e a primeira requisição falha em um `engine()` não
inicializado.

`Mount` do Starlette tem o mesmo comportamento: a aplicação montada recebe
requisições, mas o seu lifespan não é executado pelo pai.

A correção está em `ciclo_de_vida` abaixo: um `AsyncExitStack` entra no
contexto de lifespan de cada aplicação montada, na ordem, e os desfaz na ordem
inversa ao encerrar. É um detalhe de três linhas sem o qual todo o desenho de
modo duplo não funciona, e que não aparece em nenhum tutorial de `Mount`.

## Por que o Django fica montado na raiz, e por último

A ordem de `Mount` importa: o Starlette casa a primeira rota que bate. O
Django responde a `/` e a tudo abaixo, então montá-lo antes faria ele
interceptar `/api/...` também. Montado por último, recebe só o que não casou
com os prefixos de API.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.routing import Mount

from hub_core import clients
from hub_core.logging import configurar_logging, obter_logger
from hub_core.settings import obter_settings

log = obter_logger(__name__)

PREFIXO_API = "/api"


def _importar_apps() -> dict[str, FastAPI]:
    """Importa as quatro aplicações.

    Import tardio, dentro da função: importar no topo faria cada aplicação ser
    construída no momento em que este módulo é lido, inclusive quando ele é
    importado apenas para inspeção (por um teste, por exemplo).
    """
    from hub_catalog.app import app as app_catalog
    from hub_execution.app import app as app_execution
    from hub_identity.app import app as app_identity
    from hub_runner.app import app as app_runner

    return {
        clients.IDENTITY: app_identity,
        clients.CATALOG: app_catalog,
        clients.EXECUTION: app_execution,
        clients.RUNNER: app_runner,
    }


def _montar_django() -> Any | None:
    """Devolve a aplicação ASGI do portal, ou `None` se o Django não estiver instalado.

    Opcional de propósito: o gateway serve para rodar a API em um processo só,
    e isso é útil mesmo sem o portal, por exemplo em um ambiente que consome
    apenas a API.
    """
    try:
        import django  # noqa: F401
    except ImportError:
        log.warning("gateway.sem_django", dica="pip install 'orbita-hub[portal]'")
        return None

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "portal.configuracao")
    # O diretório do projeto Django precisa estar no `sys.path`; ele não é um
    # pacote instalável, é uma aplicação.
    import sys
    from pathlib import Path

    raiz_portal = Path(__file__).resolve().parents[2] / "apps" / "portal"
    if raiz_portal.is_dir() and str(raiz_portal) not in sys.path:
        sys.path.insert(0, str(raiz_portal))

    try:
        # `portal.asgi.application`, e não `get_asgi_application()` direto: a
        # primeira já embrulha o Django com o `Mount` dos arquivos estáticos
        # (ver o docstring de `portal.asgi`). Usar a segunda faria o portal
        # funcionar sozinho e ficar sem CSS quando montado aqui, o tipo de
        # divergência entre modos que este projeto existe para evitar.
        from portal.asgi import application

        return application
    except Exception:
        log.error("gateway.django_falhou", exc_info=True)
        return None


def criar_gateway() -> FastAPI:
    cfg = obter_settings()
    configurar_logging(json=cfg.log_json, nivel=cfg.log_nivel)

    if not cfg.modo_monolito:
        log.warning(
            "gateway.modo_inesperado",
            modo=cfg.hub_mode,
            dica="o gateway existe para HUB_MODE=monolith; em distributed, suba cada serviço",
        )

    apps = _importar_apps()

    # Registrar antes de qualquer requisição: é o que faz `ClienteServico`
    # escolher o transporte in-process em vez de abrir socket.
    for nome, aplicacao in apps.items():
        clients.registrar_app_embutida(nome, aplicacao)

    @contextlib.asynccontextmanager
    async def ciclo_de_vida(_app: FastAPI) -> AsyncIterator[None]:
        # Aqui está o ponto do módulo. Sem isto, nenhum serviço embutido
        # inicializa, ver docstring.
        async with contextlib.AsyncExitStack() as pilha:
            for nome, aplicacao in apps.items():
                await pilha.enter_async_context(aplicacao.router.lifespan_context(aplicacao))
                log.info("gateway.servico_pronto", servico=nome)

            log.info(
                "gateway.pronto",
                modo=cfg.hub_mode,
                servicos=list(apps),
                fila=cfg.broker_backend,
                executor=cfg.executor_backend,
            )
            yield

    gateway = FastAPI(
        title="Órbita, Gateway",
        description=__doc__,
        version="0.1.0",
        lifespan=ciclo_de_vida,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    gateway.state.settings = cfg

    @gateway.get("/_plataforma/saude", tags=["plataforma"])
    async def saude() -> dict[str, Any]:
        """Saúde agregada do processo.

        Não consulta os serviços embutidos por HTTP: eles estão neste mesmo
        processo, e uma requisição a si mesmo para responder a um health check
        chamado a cada poucos segundos é trabalho que só gera carga.
        """
        return {
            "situacao": "ok",
            "modo": cfg.hub_mode,
            "servicos": list(apps),
            "adaptadores": {
                "fila": cfg.broker_backend,
                "executor": cfg.executor_backend,
                "artefatos": cfg.storage_backend,
                "lock": cfg.lock_backend,
            },
            "portal": _portal_montado,
        }

    @gateway.get("/_plataforma/docs", include_in_schema=False)
    async def indice_de_docs() -> JSONResponse:
        """Índice dos `/docs` de cada serviço.

        Cada serviço mantém o seu próprio OpenAPI, mesclar os quatro em um
        schema só perderia a informação de qual serviço expõe o quê, que é o
        que alguém quer saber ao ler a documentação de uma plataforma de
        microsserviços.
        """
        return JSONResponse({nome: f"{PREFIXO_API}/{nome}/docs" for nome in apps})

    for nome, aplicacao in apps.items():
        gateway.mount(f"{PREFIXO_API}/{nome}", aplicacao, name=nome)

    portal = _montar_django()
    global _portal_montado
    _portal_montado = portal is not None

    if portal is not None:
        # Por último e na raiz: ver docstring sobre ordem de casamento de rotas.
        gateway.router.routes.append(Mount("/", app=portal, name="portal"))
    else:

        @gateway.get("/", include_in_schema=False)
        async def raiz() -> RedirectResponse:
            return RedirectResponse(f"{PREFIXO_API}/catalog/docs")

    return gateway


_portal_montado = False

app = criar_gateway()
