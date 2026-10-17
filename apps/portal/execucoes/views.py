"""Disparo e acompanhamento de execuções.

A tela de acompanhamento é a mais interessante do portal: ela mostra log,
métricas e artefatos de uma execução **enquanto ela roda**, sem WebSocket e sem
framework de JavaScript.

## Como o acompanhamento ao vivo funciona

HTMX com *polling* e um ponteiro de sequência:

1. a página carrega com o log que já existe e guarda a última `sequencia`;
2. a cada 2 s, o HTMX pede `/execucoes/<id>/log?desde=<sequencia>`;
3. o serviço devolve **só** as linhas novas, e o HTMX as **acrescenta** ao fim
   da lista (`hx-swap="beforeend"`);
4. quando a execução termina, o fragmento devolvido não contém o atributo de
   polling, e o HTMX simplesmente para de pedir.

O passo 3 é o que torna isso viável: sem o ponteiro, cada atualização
rebaixaria o log inteiro. Em uma execução com 2 000 linhas, seriam alguns
megabytes a cada 2 s.

O passo 4 é o detalhe que evita o bug clássico de polling: um laço que continua
rodando para sempre depois de a execução acabar. Quem decide parar é o servidor,
que é quem sabe o estado, não um contador no navegador.

## Por que não WebSocket

Seria mais eficiente e é o que a arquitetura-alvo prevê. Não está aqui por três
motivos práticos: exige processo com estado (e o free tier hiberna), exige
sessão presa à instância (*sticky session*) quando há mais de uma réplica, e
reconexão com reenvio do que foi perdido é um problema que o ponteiro de
sequência resolve de graça. Ver docs/adr/0013.
"""

from __future__ import annotations

import json
from typing import Any

from django.contrib import messages
from django.http import HttpRequest, HttpResponse, StreamingHttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from hub_core.errors import ErroOrbita
from hub_core.logging import obter_logger
from nucleo import bff
from nucleo.decoradores import e_htmx, exige

log = obter_logger("portal.execucoes")

POR_PAGINA = 20


@exige("execucao:ver_proprias")
async def listar(request: HttpRequest) -> HttpResponse:
    pagina = _inteiro(request.GET.get("pagina"), 1)
    filtros = {
        "automacao": request.GET.get("automacao") or None,
        "status": request.GET.get("status") or None,
        "origem": request.GET.get("origem") or None,
        "apenas_minhas": request.GET.get("apenas_minhas") == "1" or None,
        "pagina": pagina,
        "por_pagina": POR_PAGINA,
    }

    resultado = await bff.de(request).execucoes(
        **{c: v for c, v in filtros.items() if v is not None}
    )

    contexto = {
        "itens": resultado["itens"],
        "total": resultado["total"],
        "pagina": pagina,
        "paginas": max(1, -(-resultado["total"] // POR_PAGINA)),
        "filtros": {c: v for c, v in filtros.items() if c not in {"pagina", "por_pagina"}},
        # Há execução em andamento? Se sim, a tabela se atualiza sozinha.
        "tem_ativa": any(i["em_andamento"] for i in resultado["itens"]),
    }

    gabarito = "execucoes/_tabela.html" if e_htmx(request) else "execucoes/listar.html"
    return render(request, gabarito, contexto)


@exige("execucao:disparar")
@require_http_methods(["GET", "POST"])
async def disparar(request: HttpRequest, slug: str) -> HttpResponse:
    """Formulário de parâmetros e disparo.

    O formulário é **gerado a partir do manifesto**: os campos vêm de
    `automacao.parametros`, com tipo, rótulo, ajuda e opções. Nenhuma tela
    precisa ser escrita para uma automação nova, é a contrapartida de ter
    declarado os parâmetros no contrato.
    """
    automacao = await bff.de(request).automacao(slug)

    if not automacao.get("pode_executar"):
        messages.error(request, "Você não tem acesso de execução a esta automação.")
        return redirect("marketplace:ficha", slug=slug)

    if request.method == "GET":
        return render(request, "execucoes/disparar.html", {"automacao": automacao, "valores": {}})

    valores = _coletar_parametros(request, automacao.get("parametros") or [])

    try:
        execucao = await bff.de(request).disparar(slug, valores)
    except ErroOrbita as erro:
        # Erro de validação volta com os campos preenchidos: perder o que a
        # pessoa digitou por causa de um campo errado é o jeito mais rápido de
        # fazer alguém desistir do formulário.
        return render(
            request,
            "execucoes/disparar.html",
            {
                "automacao": automacao,
                "valores": valores,
                "erro": erro.mensagem,
                "campos_com_erro": _campos_com_erro(erro),
            },
            status=erro.status_http if erro.status_http < 500 else 400,
        )

    return redirect("execucoes:detalhe", execucao_id=execucao["id"])


def _coletar_parametros(request: HttpRequest, declarados: list[dict[str, Any]]) -> dict[str, Any]:
    """Lê do POST apenas o que o manifesto declarou.

    Iterar sobre a **declaração**, e não sobre o POST, é o que impede um campo
    injetado no formulário de chegar ao serviço. O serviço também descarta o
    que não foi declarado (ver `hub_catalog.servico.validar_parametros`), então
    são duas barreiras, mas esta é a que mantém o payload limpo.
    """
    valores: dict[str, Any] = {}
    for declaracao in declarados:
        nome = str(declaracao.get("nome") or "")
        if not nome:
            continue
        tipo = declaracao.get("tipo")

        if tipo == "multiopcao":
            escolhidos = request.POST.getlist(nome)
            if escolhidos:
                valores[nome] = escolhidos
        elif tipo == "booleano":
            # Caixa de seleção não marcada simplesmente não vem no POST, então
            # a ausência é `False`, e não "campo não informado".
            valores[nome] = request.POST.get(nome) == "on"
        else:
            bruto = (request.POST.get(nome) or "").strip()
            if bruto:
                valores[nome] = bruto
    return valores


def _campos_com_erro(erro: ErroOrbita) -> dict[str, str]:
    campos = erro.detalhes.get("campos") or []
    if not isinstance(campos, list):
        return {}
    return {
        str(item.get("campo")): str(item.get("problema"))
        for item in campos
        if isinstance(item, dict)
    }


@exige("execucao:ver_proprias")
async def detalhe(request: HttpRequest, execucao_id: str) -> HttpResponse:
    execucao = await bff.de(request).execucao(execucao_id)
    return render(
        request,
        "execucoes/detalhe.html",
        {
            "execucao": execucao,
            "parametros_json": json.dumps(
                execucao.get("parametros") or {}, ensure_ascii=False, indent=2
            ),
        },
    )


@exige("execucao:ver_proprias")
async def log_incremental(request: HttpRequest, execucao_id: str) -> HttpResponse:
    """Fragmento com as linhas de log novas. É o coração do acompanhamento.

    Devolve `_log_linhas.html`, que o HTMX acrescenta ao fim da lista. O
    fragmento inclui o gatilho do próximo poll **somente enquanto a execução
    está em andamento**, é assim que o polling para sozinho.
    """
    desde = _inteiro(request.GET.get("desde"), 0, minimo=0)
    execucao = await bff.de(request).execucao(execucao_id, desde_sequencia=desde)

    return render(
        request,
        "execucoes/_log_linhas.html",
        {
            "execucao": execucao,
            "linhas": execucao.get("logs") or [],
            "desde": execucao.get("ultima_sequencia", desde),
        },
    )


@exige("execucao:ver_proprias")
async def cartao_de_estado(request: HttpRequest, execucao_id: str) -> HttpResponse:
    """Fragmento do cabeçalho da execução: status, tempos, métricas, artefatos.

    Separado do log porque muda de forma diferente: o log só cresce (e por isso
    é acrescentado), enquanto o estado é **substituído** a cada atualização.
    Misturar os dois em um fragmento faria o log inteiro ser retransmitido a
    cada 2 s só para atualizar o rótulo de status.

    `trocar_contexto` faz a resposta carregar também o bloco de contexto, fora
    de banda, porque três campos dele mudam depois que a página carrega e ele
    não pertence a nenhum fragmento que se renova. Ver `_contexto.html`.
    """
    execucao = await bff.de(request).execucao(execucao_id, desde_sequencia=10**9)
    return render(
        request,
        "execucoes/_estado.html",
        {"execucao": execucao, "trocar_contexto": True},
    )


@exige("execucao:cancelar")
@require_http_methods(["POST"])
async def cancelar(request: HttpRequest, execucao_id: str) -> HttpResponse:
    await bff.de(request).cancelar(execucao_id)
    messages.success(request, "Execução cancelada.")
    if e_htmx(request):
        execucao = await bff.de(request).execucao(execucao_id, desde_sequencia=10**9)
        return render(
            request,
            "execucoes/_estado.html",
            {"execucao": execucao, "trocar_contexto": True},
        )
    return redirect("execucoes:detalhe", execucao_id=execucao_id)


@exige("execucao:reexecutar")
@require_http_methods(["POST"])
async def reexecutar(request: HttpRequest, execucao_id: str) -> HttpResponse:
    nova = await bff.de(request).reexecutar(execucao_id)
    messages.success(request, "Nova execução enfileirada com os mesmos parâmetros.")
    return redirect("execucoes:detalhe", execucao_id=nova["id"])


@exige("execucao:ver_proprias")
async def baixar_artefato(request: HttpRequest, execucao_id: str, artefato_id: str) -> HttpResponse:
    """Repassa o download do `execution-service` ao navegador.

    O portal fica no meio porque é ele que tem a sessão, o navegador não
    carrega o JWT. Quando o backend de armazenamento é S3, o serviço responde
    com um redirecionamento para uma URL pré-assinada e o navegador baixa
    direto do object storage; aqui, isso aparece como um 307 que é repassado.
    """
    from hub_core.clients import EXECUTION, cliente_http_bruto

    caminho = f"/execucoes/{execucao_id}/artefatos/{artefato_id}"
    token = request.session.get("token")

    cliente = cliente_http_bruto(EXECUTION)
    resposta = await cliente.get(
        caminho,
        headers={"authorization": f"Bearer {token}"} if token else {},
        follow_redirects=False,
    )

    if resposta.status_code in {301, 302, 307, 308}:
        return redirect(resposta.headers["location"])

    if resposta.status_code != 200:
        messages.error(request, "Não foi possível baixar o artefato.")
        return redirect("execucoes:detalhe", execucao_id=execucao_id)

    saida = StreamingHttpResponse(
        iter([resposta.content]),
        content_type=resposta.headers.get("content-type", "application/octet-stream"),
    )
    if disposicao := resposta.headers.get("content-disposition"):
        saida["content-disposition"] = disposicao
    return saida


def _inteiro(valor: Any, padrao: int, *, minimo: int = 1) -> int:
    try:
        return max(minimo, int(valor))
    except (TypeError, ValueError):
        return padrao
