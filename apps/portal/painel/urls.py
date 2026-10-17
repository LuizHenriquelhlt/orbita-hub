from __future__ import annotations

from django.urls import path

from painel import views

app_name = "painel"

urlpatterns = [
    path("", views.painel, name="painel"),
]
