"""Área administrativa: pessoas, auditoria, filas e runners.

Cada tela exige a sua própria permissão, não "ser administrador". A diferença
importa: o auditor entra em *Auditoria* sem ver *Pessoas*, e o supervisor vê
*Filas* sem ver a trilha. Um único portão de "admin" transformaria qualquer
acesso à área em acesso a tudo que ela contém.
"""

from __future__ import annotations

import contextlib
from typing import Any

from django.contrib import messages
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from hub_core.errors import ErroOrbita
from nucleo import bff
from nucleo.decoradores import e_htmx, exige, recusa_demonstracao

POR_PAGINA = 25


@exige("usuario:ver")
async def pessoas(request: HttpRequest) -> HttpResponse:
    pagina = _inteiro(request.GET.get("pagina"), 1)
    filtros: dict[str, Any] = {
        "busca": request.GET.get("busca") or None,
        "papel": request.GET.get("papel") or None,
        "unidade_id": request.GET.get("unidade_id") or None,
        "apenas_ativos": request.GET.get("inativos") != "1",
        "pagina": pagina,
        "por_pagina": POR_PAGINA,
    }

    resultado = await bff.de(request).usuarios(
        **{c: v for c, v in filtros.items() if v is not None}
    )

    # A lista de unidades alimenta um filtro opcional. Se o identity estiver
    # fora do ar, a tela abre sem o filtro em vez de não abrir, uma página
    # incompleta é melhor do que uma página que não carrega porque um
    # complemento falhou.
    unidades: list[dict[str, Any]] = []
    with contextlib.suppress(ErroOrbita):
        unidades = await bff.de(request).unidades()

    contexto = {
        "itens": resultado["itens"],
        "total": resultado["total"],
        "pagina": pagina,
        "paginas": max(1, -(-resultado["total"] // POR_PAGINA)),
        "unidades": unidades,
        "filtros": {c: v for c, v in filtros.items() if c not in {"pagina", "por_pagina"}},
    }

    gabarito = (
        "administracao/_pessoas_tabela.html" if e_htmx(request) else "administracao/pessoas.html"
    )
    return render(request, gabarito, contexto)


@exige("auditoria:ver")
async def auditoria(request: HttpRequest) -> HttpResponse:
    pagina = _inteiro(request.GET.get("pagina"), 1)
    filtros: dict[str, Any] = {
        "acao": request.GET.get("acao") or None,
        "recurso": request.GET.get("recurso") or None,
        "resultado": request.GET.get("resultado") or None,
        "pagina": pagina,
        "por_pagina": 50,
    }

    resultado = await bff.de(request).auditoria(
        **{c: v for c, v in filtros.items() if v is not None}
    )

    contexto = {
        "itens": resultado["itens"],
        "total": resultado["total"],
        "pagina": pagina,
        "paginas": max(1, -(-resultado["total"] // 50)),
        "filtros": {c: v for c, v in filtros.items() if c not in {"pagina", "por_pagina"}},
        # Os prefixos de ação mais comuns, para o filtro rápido. Lista fixa
        # porque é vocabulário do domínio, não dado, ver `hub_core.events`.
        "acoes_comuns": [
            ("login", "Entradas"),
            ("execucao", "Execuções"),
            ("automacao", "Automações"),
            ("usuario", "Pessoas"),
            ("acesso", "Concessões de acesso"),
            ("fila", "Filas"),
        ],
    }

    gabarito = (
        "administracao/_auditoria_tabela.html"
        if e_htmx(request)
        else "administracao/auditoria.html"
    )
    return render(request, gabarito, contexto)


@exige("fila:ver")
async def filas(request: HttpRequest) -> HttpResponse:
    """Profundidade das filas e a fila de mensagens mortas.

    A inspeção da DLQ só existe com `BROKER_BACKEND=postgres`, com RabbitMQ,
    quem faz isso é o painel de gestão dele, e reimplementá-lo aqui seria
    recriar uma ferramenta que já existe e é melhor. A tela diz isso em vez de
    mostrar uma lista vazia sem explicação.
    """
    lista = await bff.de(request).filas()

    mortas: list[dict[str, Any]] = []
    aviso_dlq = ""
    try:
        mortas = await bff.de(request).mensagens_mortas()
    except ErroOrbita as erro:
        aviso_dlq = erro.mensagem

    return render(
        request,
        "administracao/filas.html",
        {"filas": lista, "mortas": mortas, "aviso_dlq": aviso_dlq},
    )


@exige("fila:administrar")
@recusa_demonstracao
@require_http_methods(["POST"])
async def reviver_mensagem(request: HttpRequest, mensagem_id: str) -> HttpResponse:
    resultado = await bff.de(request).reviver_mensagem(mensagem_id)
    if resultado.get("revivida"):
        messages.success(request, "Mensagem devolvida à fila, com as tentativas zeradas.")
    else:
        messages.warning(request, "A mensagem não estava mais na fila de mortas.")
    return redirect("administracao:filas")


@exige("runner:ver")
async def runners(request: HttpRequest) -> HttpResponse:
    """Situação do runner e os containers que ele gerencia.

    A lista de containers só existe com `EXECUTOR_BACKEND=docker`. Com o
    executor simulado ou de subprocesso não há container, e a tela explica isso
    em vez de mostrar uma tabela vazia.
    """
    situacao = await bff.de(request).situacao_do_runner()

    containers: list[dict[str, Any]] = []
    aviso = ""
    try:
        containers = await bff.de(request).containers()
    except ErroOrbita as erro:
        aviso = erro.mensagem

    return render(
        request,
        "administracao/runners.html",
        {"situacao": situacao, "containers": containers, "aviso": aviso},
    )


def _inteiro(valor: Any, padrao: int) -> int:
    try:
        return max(1, int(valor))
    except (TypeError, ValueError):
        return padrao
