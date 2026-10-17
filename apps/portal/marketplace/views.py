"""O catálogo de automações: listagem, ficha e ciclo de vida."""

from __future__ import annotations

from typing import Any

from django.contrib import messages
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from nucleo import bff
from nucleo.decoradores import exige, recusa_demonstracao

POR_PAGINA = 12


@exige("automacao:ver")
async def catalogo(request: HttpRequest) -> HttpResponse:
    """A home: cartões de automação, com busca e filtros.

    Os filtros vão na query string, não em sessão nem em POST. Com isso o
    estado da tela é a URL: dá para compartilhar um link de "automações de
    relatório da categoria X", voltar no navegador funciona, e o botão de
    recarregar não reenvia formulário.
    """
    pagina = _inteiro(request.GET.get("pagina"), 1)
    filtros = {
        "busca": request.GET.get("busca") or None,
        "categoria": request.GET.get("categoria") or None,
        "tipo": request.GET.get("tipo") or None,
        "tag": request.GET.get("tag") or None,
        "status": request.GET.get("status") or None,
        "pagina": pagina,
        "por_pagina": POR_PAGINA,
    }

    resultado = await bff.de(request).catalogo(
        **{c: v for c, v in filtros.items() if v is not None}
    )

    contexto = {
        "itens": resultado["itens"],
        "total": resultado["total"],
        "facetas": resultado.get("facetas", {}),
        "pagina": pagina,
        "paginas": _total_de_paginas(resultado["total"], POR_PAGINA),
        "filtros": {c: v for c, v in filtros.items() if c not in {"pagina", "por_pagina"}},
        "tem_filtro": any(filtros[c] for c in ("busca", "categoria", "tipo", "tag", "status")),
    }

    # Requisição do HTMX recebe apenas a grade, não a página inteira: é o que
    # faz a busca atualizar sem recarregar o cabeçalho e os filtros.
    gabarito = (
        "marketplace/_grade.html"
        if request.headers.get("hx-request") == "true"
        else "marketplace/catalogo.html"
    )
    return render(request, gabarito, contexto)


@exige("automacao:ver")
async def ficha(request: HttpRequest, slug: str) -> HttpResponse:
    """Ficha da automação: o que ela é, mais como tem se comportado.

    Duas chamadas a serviços diferentes, feitas em paralelo — ver
    `bff.Bff.ficha_completa`. É aqui que a separação entre `catalog` e
    `execution` aparece para quem usa: o catálogo não sabe quantas vezes a
    automação rodou.
    """
    automacao, estatisticas = await bff.de(request).ficha_completa(slug)

    # Últimas execuções desta automação, para o histórico na própria ficha.
    execucoes: list[dict[str, Any]] = []
    try:
        resultado = await bff.de(request).execucoes(automacao=slug, por_pagina=8)
        execucoes = resultado.get("itens", [])
    except Exception:
        pass

    return render(
        request,
        "marketplace/ficha.html",
        {
            "automacao": automacao,
            "estatisticas": estatisticas,
            "execucoes": execucoes,
        },
    )


@exige("automacao:criar")
@recusa_demonstracao
@require_http_methods(["GET", "POST"])
async def importar(request: HttpRequest) -> HttpResponse:
    """Cadastra uma automação colando o `automation.yaml`.

    É a tela que materializa o contrato de plugin: nenhuma alteração no código
    da plataforma é necessária para registrar uma automação nova. A alternativa
    — um formulário com 30 campos espelhando o manifesto — teria de ser
    reescrita a cada campo novo do contrato.
    """
    contexto: dict[str, Any] = {"yaml": "", "publicar": False}

    if request.method == "GET":
        return render(request, "marketplace/importar.html", contexto)

    yaml_texto = request.POST.get("manifesto_yaml") or ""
    publicar = request.POST.get("publicar") == "on"
    contexto.update({"yaml": yaml_texto, "publicar": publicar})

    if not yaml_texto.strip():
        contexto["erro"] = "Cole o conteúdo do automation.yaml."
        return render(request, "marketplace/importar.html", contexto, status=400)

    from hub_core.errors import EntradaInvalida

    try:
        resultado = await bff.de(request).importar_manifesto(yaml_texto, publicar)
    except EntradaInvalida as erro:
        # Os problemas vêm campo por campo do validador do manifesto. Mostrá-los
        # na tela, em lista, é a diferença entre corrigir o YAML em um minuto e
        # abrir um chamado.
        contexto["erro"] = erro.mensagem
        contexto["problemas"] = erro.detalhes.get("problemas") or []
        return render(request, "marketplace/importar.html", contexto, status=422)

    verbo = "cadastrada" if resultado["criada"] else "atualizada"
    messages.success(
        request,
        f"Automação {resultado['slug']} {verbo} na versão {resultado['versao']} "
        f"(status: {resultado['status']}).",
    )
    for aviso in resultado.get("avisos") or []:
        messages.warning(request, aviso)

    return redirect("marketplace:ficha", slug=resultado["slug"])


@exige()
@recusa_demonstracao
@require_http_methods(["POST"])
async def transicionar(request: HttpRequest, slug: str) -> HttpResponse:
    """Move a automação no ciclo de vida.

    A permissão exigida depende da transição, então não há `exige(...)`
    específico aqui — é o `catalog-service` que consulta a tabela de
    transições e decide. Duplicar a regra no portal criaria duas respostas
    possíveis para a mesma pergunta.
    """
    para = request.POST.get("para") or ""
    justificativa = request.POST.get("justificativa") or ""

    automacao = await bff.de(request).transicionar(slug, para, justificativa)
    messages.success(
        request, f"{automacao['nome']} agora está em {automacao['status_rotulo'].lower()}."
    )
    return redirect("marketplace:ficha", slug=slug)


def _inteiro(valor: Any, padrao: int) -> int:
    try:
        return max(1, int(valor))
    except (TypeError, ValueError):
        return padrao


def _total_de_paginas(total: int, por_pagina: int) -> int:
    return max(1, -(-total // por_pagina))
