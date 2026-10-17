"""Aplicação ASGI do `identity-service`.

Rodar sozinho:

    uvicorn hub_identity.app:app --port 8001

Em modo monolito, `hub_gateway.main` importa `app` daqui e o monta em
`/api/identity`, sem subir processo próprio.
"""

from __future__ import annotations

from fastapi import FastAPI

from hub_core import auditoria, db
from hub_core.service import criar_app
from hub_identity.rotas import montar

DESCRICAO = """
Usuários, unidades organizacionais, grupos, papéis e a trilha de auditoria.

É o único serviço que **emite** JWT. Os outros três apenas validam a assinatura
e leem os claims, sem consultar este serviço, então o catálogo e as execuções
continuam funcionando se este serviço cair. A contrapartida é que retirar um
papel de alguém só tem efeito quando o token dela expira (60 min por padrão).
"""


def construir() -> FastAPI:
    aplicacao = criar_app(
        nome="identity",
        titulo="Órbita, Identidade e Acesso",
        descricao=DESCRICAO,
        router=montar(),
        ao_iniciar=lambda _app: auditoria.assinar_eventos(
            db.fabrica_de_sessoes(), servico="identity"
        ),
    )
    return aplicacao


app = construir()
