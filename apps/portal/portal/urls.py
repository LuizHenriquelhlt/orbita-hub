"""Rotas do portal.

Uma `include` por domínio, cada um em seu app. Rota de domínio dentro deste
arquivo seria o começo de um `urls.py` de 300 linhas que ninguém lê.
"""

from __future__ import annotations

from django.urls import include, path

from nucleo import views as views_nucleo

urlpatterns = [
    path("", include("marketplace.urls")),
    path("", include("contas.urls")),
    path("execucoes/", include("execucoes.urls")),
    path("painel/", include("painel.urls")),
    path("administracao/", include("administracao.urls")),
    # Verificação de saúde do portal. Não consulta os serviços: o health check
    # do orquestrador roda a cada poucos segundos, e encadear a saúde do portal
    # à dos quatro serviços faria uma indisponibilidade de qualquer um deles
    # derrubar o portal também, exatamente quando ele é mais necessário para
    # mostrar o que está acontecendo.
    path("saude", views_nucleo.saude, name="saude"),
]

handler403 = "nucleo.views.erro_403"
handler404 = "nucleo.views.erro_404"
handler500 = "nucleo.views.erro_500"
