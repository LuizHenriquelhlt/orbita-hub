from __future__ import annotations

from django.urls import path

from execucoes import views

app_name = "execucoes"

urlpatterns = [
    path("", views.listar, name="listar"),
    path("disparar/<slug:slug>", views.disparar, name="disparar"),
    path("<uuid:execucao_id>", views.detalhe, name="detalhe"),
    # Fragmentos do acompanhamento ao vivo. Separados porque mudam de formas
    # diferentes: o log é acrescentado, o estado é substituído.
    path("<uuid:execucao_id>/log", views.log_incremental, name="log"),
    path("<uuid:execucao_id>/estado", views.cartao_de_estado, name="estado"),
    path("<uuid:execucao_id>/cancelar", views.cancelar, name="cancelar"),
    path("<uuid:execucao_id>/reexecutar", views.reexecutar, name="reexecutar"),
    path(
        "<uuid:execucao_id>/artefatos/<uuid:artefato_id>",
        views.baixar_artefato,
        name="baixar_artefato",
    ),
]
