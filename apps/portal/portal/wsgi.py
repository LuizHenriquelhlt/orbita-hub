"""Entrada WSGI, mantida para compatibilidade com ferramenta que a exija.

O portal roda sob ASGI (`portal.asgi`). Este arquivo existe porque alguns
utilitários e plataformas de deploy ainda procuram por um callable WSGI, e
porque o `collectstatic` não se importa com qual dos dois está configurado.

Servir o portal por aqui funciona, mas as views assíncronas passam a rodar em
pool de threads e as chamadas paralelas aos serviços perdem o paralelismo.
"""

from __future__ import annotations

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "portal.configuracao")

application = get_wsgi_application()
