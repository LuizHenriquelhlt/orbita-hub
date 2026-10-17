"""Entrada ASGI do portal: Django mais os arquivos estáticos.

    uvicorn portal.asgi:application --app-dir apps/portal

## Por que os estáticos não passam pelo Django

A forma usual é a middleware do WhiteNoise. Ela não serve aqui por um motivo
concreto: a versão atual do WhiteNoise **não declara `async_capable`**, então o
Django a trata como middleware somente síncrona, e uma middleware síncrona na
cadeia faz o Django adaptar todo o resto para modo síncrono, incluindo as
views. As views assíncronas passariam a rodar em pool de threads, e o
transporte in-process do modo monolito (que é `await` puro) quebraria com
`'coroutine' object has no attribute 'get'`.

Em vez de contornar isso, os estáticos saem da cadeia: um `Mount` do Starlette
em `/static`, nativo assíncrono, antes do Django. O resultado é melhor nos dois
sentidos, nenhum pedido de CSS entra no ciclo de middleware do Django, e a
cadeia continua assíncrona de ponta a ponta.

## Por que ASGI e não WSGI

As views são `async def`, exigência do transporte in-process do modo monolito
(ver `nucleo.bff`). Sob WSGI, o Django embrulharia cada uma em pool de threads,
desperdiçando o paralelismo das chamadas aos serviços.
"""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "portal.configuracao")

from django.core.asgi import get_asgi_application

aplicacao_django = get_asgi_application()

BASE_DIR = Path(__file__).resolve().parent.parent


def _diretorio_de_estaticos() -> Path:
    """Onde os arquivos estáticos estão.

    Em produção, em `staticfiles/`, produzido por `collectstatic` com os nomes
    já contendo o hash do conteúdo. Em desenvolvimento, direto de `static/`,
    para que salvar o CSS tenha efeito sem rodar comando nenhum.

    A ordem da verificação importa: se `staticfiles/` existe, ele ganha,
    porque é o que o `{% static %}` vai referenciar quando o
    `ManifestStaticFilesStorage` estiver ativo.
    """
    coletados = BASE_DIR / "staticfiles"
    if coletados.is_dir() and any(coletados.iterdir()):
        return coletados
    return BASE_DIR / "static"


def _montar() -> object:
    """Compõe estáticos + Django. Degrada para o Django puro sem Starlette."""
    try:
        from starlette.applications import Starlette
        from starlette.routing import Mount
        from starlette.staticfiles import StaticFiles
    except ImportError:
        # Starlette vem com o FastAPI, que é dependência do núcleo. Se por
        # algum motivo não estiver presente, o portal ainda sobe, só sem
        # servir estáticos, o que em produção é tarefa de um CDN de qualquer
        # forma.
        return aplicacao_django

    diretorio = _diretorio_de_estaticos()

    return Starlette(
        routes=[
            Mount(
                "/static",
                app=StaticFiles(directory=str(diretorio), check_dir=False),
                name="static",
            ),
            # O Django por último e na raiz: o Starlette casa a primeira rota
            # que bate, então o Django montado antes interceptaria `/static`
            # também.
            Mount("/", app=aplicacao_django),
        ]
    )


application = _montar()
