from __future__ import annotations

from django.urls import path

from contas import views

app_name = "contas"

urlpatterns = [
    path("entrar", views.entrar, name="entrar"),
    path("entrar/demonstracao", views.entrar_demonstracao, name="entrar_demonstracao"),
    path("sair", views.sair, name="sair"),
    path("perfil", views.perfil, name="perfil"),
]
