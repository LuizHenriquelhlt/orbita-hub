"""Entrada e saída do portal."""

from __future__ import annotations

from django.conf import settings
from django.contrib import messages
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from hub_core.errors import ErroOrbita, NaoAutenticado
from hub_core.logging import obter_logger
from nucleo import bff
from nucleo.decoradores import exige
from nucleo.middleware import guardar_na_sessao

log = obter_logger("portal.contas")


def _destino_seguro(request: HttpRequest) -> str:
    """Valida o `?proximo=` antes de redirecionar para ele.

    Redirecionar para uma URL que veio da query string sem conferir é *open
    redirect*: um link para `/entrar?proximo=https://site-falso/` leva a pessoa
    a autenticar no portal verdadeiro e cair numa página de terceiro, que então
    pede a senha "de novo". Só caminho relativo é aceito.
    """
    proximo = request.GET.get("proximo") or request.POST.get("proximo") or ""
    if proximo.startswith("/") and not proximo.startswith("//"):
        return proximo
    return reverse("marketplace:catalogo")


@require_http_methods(["GET", "POST"])
async def entrar(request: HttpRequest) -> HttpResponse:
    quem = getattr(request, "quem", None)
    if quem is not None and quem.autenticado and request.method == "GET":
        return redirect(_destino_seguro(request))

    contexto: dict[str, object] = {
        "proximo": request.GET.get("proximo", ""),
        "expirou": request.GET.get("expirou") == "1",
        "papeis": [],
    }

    if settings.DEMO_MODE:
        try:
            # A rota de papéis é aberta; se o identity estiver fora do ar, a
            # tela de entrada por senha continua funcionando sem os botões de
            # demonstração.
            contexto["papeis"] = await bff.Bff(None).papeis()
        except ErroOrbita:
            log.warning("portal.papeis_indisponiveis")

    if request.method == "GET":
        return render(request, "contas/entrar.html", contexto)

    cpf = (request.POST.get("cpf") or "").strip()
    senha = request.POST.get("senha") or ""

    if not cpf or not senha:
        contexto["erro"] = "Informe CPF e senha."
        contexto["cpf"] = cpf
        return render(request, "contas/entrar.html", contexto, status=400)

    try:
        resposta = await bff.Bff(None).login(cpf, senha)
    except NaoAutenticado as erro:
        # A mensagem vem do serviço, que a mantém igual para CPF inexistente e
        # senha errada — ver `hub_identity.servico.autenticar`. Repeti-la aqui
        # sem alterar é o que preserva essa propriedade.
        contexto["erro"] = erro.mensagem
        contexto["cpf"] = cpf
        return render(request, "contas/entrar.html", contexto, status=401)
    except ErroOrbita as erro:
        contexto["erro"] = f"Não foi possível entrar agora: {erro.mensagem}"
        contexto["cpf"] = cpf
        return render(request, "contas/entrar.html", contexto, status=503)

    guardar_na_sessao(request.session, resposta)
    log.info("portal.login", usuario=resposta["usuario"]["nome"])
    return redirect(_destino_seguro(request))


@require_http_methods(["POST"])
async def entrar_demonstracao(request: HttpRequest) -> HttpResponse:
    """Entra assumindo um papel, sem senha. Só com `DEMO_MODE=true`."""
    if not settings.DEMO_MODE:
        return redirect("contas:entrar")

    papel = (request.POST.get("papel") or "").strip()
    try:
        resposta = await bff.Bff(None).login_demonstracao(papel)
    except ErroOrbita as erro:
        messages.error(request, erro.mensagem)
        return redirect("contas:entrar")

    resposta["demonstracao"] = True
    guardar_na_sessao(request.session, resposta)
    messages.info(
        request,
        f"Você está navegando como {papel} em uma sessão de demonstração. "
        "Cadastros são somente leitura; disparar automação está liberado.",
    )
    return redirect(_destino_seguro(request))


@require_http_methods(["POST", "GET"])
async def sair(request: HttpRequest) -> HttpResponse:
    """Encerra a sessão.

    Aceita GET além de POST, e vale registrar por quê: o correto é só POST,
    porque sair é uma ação com efeito e um GET pode ser disparado por
    *prefetch* do navegador. Aqui o GET é aceito porque a sessão não tem
    nenhum efeito colateral destrutivo ao ser destruída — e porque um link de
    sair que não funciona sem JavaScript é pior. O botão no cabeçalho usa POST
    com CSRF.
    """
    request.session.flush()
    messages.success(request, "Sessão encerrada.")
    return redirect("contas:entrar")


@exige()
async def perfil(request: HttpRequest) -> HttpResponse:
    """Os dados da própria conta, buscados ao vivo do `identity-service`.

    Ao vivo, e não do resumo da sessão: esta é a tela em que alguém confere os
    próprios papéis, e mostrar um resumo possivelmente desatualizado aqui
    seria responder a pergunta errada.
    """
    detalhe = await bff.de(request).eu()
    return render(request, "contas/perfil.html", {"detalhe": detalhe})
