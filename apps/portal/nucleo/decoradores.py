"""Decoradores de view: exigem sessão e permissão, e traduzem erro de domínio.

O `exige` abaixo faz três coisas que, somadas, eliminam quase todo `try/except`
das views:

1. manda para a tela de entrada quem não tem sessão, guardando o destino para
   voltar depois;
2. recusa com 403 quem não tem a permissão — **para esconder a tela**; a
   autorização que conta é a do serviço (ver `nucleo.middleware`);
3. captura `ErroOrbita`, transforma em mensagem na interface e redireciona, em
   vez de deixar a página estourar.

O item 3 tem uma sutileza que vale explicar. Quando a requisição vem do HTMX,
redirecionar não funciona como se espera: o HTMX troca apenas o fragmento, e a
resposta do redirecionamento seria inserida **dentro** do pedaço de página que
disparou a requisição. Por isso o tratamento responde com um fragmento de erro
e, quando é preciso navegar, usa o cabeçalho `HX-Redirect`, que o HTMX
interpreta como "navegue a página inteira".
"""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote

from django.conf import settings
from django.contrib import messages
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render

from hub_core.errors import ErroOrbita, NaoAutenticado, SemPermissao
from hub_core.logging import obter_logger

log = obter_logger("portal")

View = Callable[..., Awaitable[HttpResponse]]


def e_htmx(request: HttpRequest) -> bool:
    return request.headers.get("hx-request") == "true"


def redirecionar_htmx(destino: str) -> HttpResponse:
    """Resposta que faz o HTMX navegar a página inteira.

    Um 302 comum seria seguido pelo próprio HTMX, que inseriria a página de
    destino dentro do fragmento — resultando em uma página dentro da outra.
    `HX-Redirect` instrui o HTMX a trocar a URL do navegador.
    """
    resposta = HttpResponse(status=204)
    resposta["HX-Redirect"] = destino
    return resposta


def exige(*permissoes: str) -> Callable[[View], View]:
    """Exige sessão e, opcionalmente, permissões.

    `exige()` sem argumento exige apenas estar autenticado.
    """

    def decorador(view: View) -> View:
        @functools.wraps(view)
        async def embrulho(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
            quem = getattr(request, "quem", None)

            if quem is None or not quem.autenticado:
                destino = f"{settings.LOGIN_URL}?proximo={quote(request.get_full_path())}"
                if e_htmx(request):
                    return redirecionar_htmx(destino)
                return redirect(destino)

            faltando = [p for p in permissoes if not quem.pode(p)]
            if faltando:
                log.info(
                    "portal.sem_permissao",
                    caminho=request.path,
                    faltando=faltando,
                    papeis=list(quem.papeis),
                )
                return render(
                    request,
                    "erros/403.html",
                    {"permissoes_faltando": faltando},
                    status=403,
                )

            try:
                return await view(request, *args, **kwargs)
            except NaoAutenticado:
                # O token expirou entre o carregamento da página e o clique.
                # Limpar a sessão e mandar para a entrada é melhor do que
                # mostrar "não autenticado" em uma tela que a pessoa estava
                # usando normalmente.
                request.session.flush()
                destino = f"{settings.LOGIN_URL}?expirou=1"
                if e_htmx(request):
                    return redirecionar_htmx(destino)
                return redirect(destino)
            except SemPermissao as erro:
                return render(
                    request,
                    "erros/403.html",
                    {"mensagem": erro.mensagem, "detalhes": erro.detalhes},
                    status=403,
                )
            except ErroOrbita as erro:
                return _tratar_erro_de_dominio(request, erro)

        return embrulho

    return decorador


def _tratar_erro_de_dominio(request: HttpRequest, erro: ErroOrbita) -> HttpResponse:
    log.warning(
        "portal.erro_de_dominio",
        caminho=request.path,
        codigo=erro.codigo,
        mensagem=erro.mensagem[:200],
    )

    if e_htmx(request):
        # Fragmento, não página: é inserido no lugar que disparou a ação.
        return render(
            request,
            "componentes/alerta.html",
            {"tipo": "erro", "mensagem": erro.mensagem, "detalhes": erro.detalhes},
            status=erro.status_http if erro.status_http < 500 else 200,
        )

    messages.error(request, erro.mensagem)
    if erro.status_http == 404:
        return render(request, "erros/404.html", {"mensagem": erro.mensagem}, status=404)

    destino = request.META.get("HTTP_REFERER") or "/"
    return redirect(destino)


def recusa_demonstracao(view: View) -> View:
    """Barra ação de escrita em sessão de demonstração, antes de chamar o serviço.

    O serviço já recusa; isto existe para que a mensagem seja útil em vez de
    um 403 genérico, e para não gastar uma chamada de rede com um pedido que
    vai ser negado.
    """

    @functools.wraps(view)
    async def embrulho(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        quem = getattr(request, "quem", None)
        if quem is not None and quem.demonstracao:
            mensagem = (
                "A sessão de demonstração não altera cadastros. "
                "Disparar automação e acompanhar execução continuam liberados."
            )
            if e_htmx(request):
                return render(
                    request,
                    "componentes/alerta.html",
                    {"tipo": "aviso", "mensagem": mensagem},
                    status=200,
                )
            messages.warning(request, mensagem)
            return redirect(request.META.get("HTTP_REFERER") or "/")
        return await view(request, *args, **kwargs)

    return embrulho
