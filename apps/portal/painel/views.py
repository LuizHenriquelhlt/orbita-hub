"""Painel de indicadores."""

from __future__ import annotations

from django.http import HttpRequest, HttpResponse
from django.shortcuts import render

from nucleo import bff
from nucleo.decoradores import e_htmx, exige


@exige("dashboard:ver")
async def painel(request: HttpRequest) -> HttpResponse:
    """Indicadores da plataforma.

    Uma chamada só: o `execution-service` já devolve contagens, série
    temporal, automações mais usadas, profundidade das filas e agendamentos em
    um objeto — porque a tela mostra tudo isso de uma vez, e cinco endpoints
    significariam cinco varreduras da mesma tabela.
    """
    indicadores = await bff.de(request).indicadores()

    # O máximo da série alimenta a altura relativa das barras. Calculado aqui
    # e não no template: o sistema de templates do Django não tem `max` sobre
    # lista de dicionários, e fazê-lo com `{% for %}` seria ilegível.
    serie = indicadores.get("serie_14d") or []
    maximo = max((d["sucesso"] + d["falha"] + d["outras"] for d in serie), default=0)

    contexto = {
        "ind": indicadores,
        "serie": serie,
        "maximo_da_serie": maximo or 1,
        # Se há execução em andamento, o painel se atualiza sozinho. Quando
        # não há, para de pedir — mesma lógica do acompanhamento de execução.
        "tem_ativa": (indicadores.get("ativas") or 0) > 0,
    }

    gabarito = "painel/_conteudo.html" if e_htmx(request) else "painel/painel.html"
    return render(request, gabarito, contexto)
