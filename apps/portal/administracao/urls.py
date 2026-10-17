from __future__ import annotations

from django.urls import path

from administracao import views

app_name = "administracao"

urlpatterns = [
    path("pessoas", views.pessoas, name="pessoas"),
    path("auditoria", views.auditoria, name="auditoria"),
    path("filas", views.filas, name="filas"),
    path("filas/mortas/<uuid:mensagem_id>/reviver", views.reviver_mensagem, name="reviver"),
    path("runners", views.runners, name="runners"),
]
