"""Processadores de contexto: o que todo template recebe sem pedir.

Substituem `django.contrib.auth.context_processors.auth`, que não existe aqui
porque o portal não usa o `auth` do Django, a identidade vem do
`identity-service` (ver `portal.configuracao`).
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.http import HttpRequest

from nucleo.middleware import ANONIMO


def identidade(request: HttpRequest) -> dict[str, Any]:
    """Põe `quem` em todo template, o equivalente ao `user` do Django."""
    return {"quem": getattr(request, "quem", ANONIMO)}


def plataforma(request: HttpRequest) -> dict[str, Any]:
    """Configuração que a interface precisa conhecer.

    `ambiente` e `modo` aparecem na faixa de identificação do rodapé. Em um
    projeto de portfólio isso é parte da demonstração: quem abre o endereço
    público vê, escrito na tela, que está no modo monolito com executor
    simulado, e não precisa adivinhar o que é real.
    """
    del request
    return {
        "AMBIENTE": settings.AMBIENTE,
        "HUB_MODE": settings.HUB_MODE,
        "DEMO_MODE": settings.DEMO_MODE,
        "INTERVALO_POLL_MS": settings.INTERVALO_POLL_MS,
        "DEBUG": settings.DEBUG,
    }
