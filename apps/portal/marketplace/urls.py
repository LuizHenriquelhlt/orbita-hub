from __future__ import annotations

from django.urls import path

from marketplace import views

app_name = "marketplace"

urlpatterns = [
    path("", views.catalogo, name="catalogo"),
    path("automacoes/importar", views.importar, name="importar"),
    # A rota de slug vem por último: `importar` é um caminho literal e seria
    # capturado como slug se viesse depois.
    path("automacoes/<slug:slug>", views.ficha, name="ficha"),
    path("automacoes/<slug:slug>/transicao", views.transicionar, name="transicionar"),
]
