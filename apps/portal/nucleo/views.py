"""Views compartilhadas: saúde e páginas de erro."""

from __future__ import annotations

from django.conf import settings
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render


async def saude(request: HttpRequest) -> JsonResponse:
    """Liveness do portal, sem tocar nos serviços. Ver `portal.urls`."""
    del request
    return JsonResponse(
        {
            "situacao": "ok",
            "componente": "portal",
            "modo": settings.HUB_MODE,
            "ambiente": settings.AMBIENTE,
        }
    )


def erro_403(request: HttpRequest, exception: Exception | None = None) -> HttpResponse:
    del exception
    return render(request, "erros/403.html", status=403)


def erro_404(request: HttpRequest, exception: Exception | None = None) -> HttpResponse:
    del exception
    return render(request, "erros/404.html", status=404)


def erro_500(request: HttpRequest) -> HttpResponse:
    """Página de 500.

    Renderizada com `render` e sem contexto extra de propósito: se o erro foi
    em um processador de contexto, qualquer coisa que esta página tentasse
    carregar estouraria de novo — e aí o Django cai para a página de erro
    padrão, crua.
    """
    return render(request, "erros/500.html", status=500)
