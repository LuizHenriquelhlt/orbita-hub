#!/usr/bin/env python
"""Utilitário de linha de comando do Django.

    python apps/portal/manage.py collectstatic --noinput
    python apps/portal/manage.py check

Comandos que dependem de banco (`migrate`, `createsuperuser`, `dbshell`) não
funcionam, e isso é esperado: o portal não tem banco. Ver
`portal.configuracao`.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "portal.configuracao")

    # Garante que `apps/portal` está no `sys.path` mesmo quando o comando é
    # chamado da raiz do repositório, que é como ele aparece no Makefile e no
    # Dockerfile.
    aqui = Path(__file__).resolve().parent
    if str(aqui) not in sys.path:
        sys.path.insert(0, str(aqui))

    try:
        from django.core.management import execute_from_command_line
    except ImportError as erro:
        raise ImportError(
            "Django não encontrado. Instale com: pip install 'orbita-hub[portal]'"
        ) from erro

    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
