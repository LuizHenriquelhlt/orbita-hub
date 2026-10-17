"""Gera `docs/automation-schema.json` a partir dos modelos do manifesto.

    python scripts/gerar_schema.py

O schema é **derivado**, não mantido à mão. A validação do manifesto, a
geração do formulário de parâmetros no portal e o autocompletar no editor de
quem escreve a automação saem todos dos mesmos modelos Pydantic — uma fonte,
três usos.

Mantido à mão, o JSON Schema sairia de sincronia com o validador no primeiro
campo novo, e o editor passaria a aceitar manifesto que o serviço recusa.

O arquivo é versionado para que quem escreve uma automação possa apontar o
editor para ele sem ter a plataforma rodando:

    # yaml-language-server: $schema=https://raw.githubusercontent.com/<voce>/orbita-hub/main/docs/automation-schema.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from hub_catalog.manifesto import gerar_json_schema  # noqa: E402

DESTINO = RAIZ / "docs" / "automation-schema.json"


def main() -> None:
    esquema = gerar_json_schema()

    DESTINO.parent.mkdir(parents=True, exist_ok=True)
    DESTINO.write_text(
        json.dumps(esquema, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    definicoes = len(esquema.get("$defs", {}))
    print(f"{DESTINO.relative_to(RAIZ)}")
    print(f"  {definicoes} definições, {DESTINO.stat().st_size / 1024:.1f} KB")
    print("  derivado dos modelos — não edite à mão")


if __name__ == "__main__":
    main()
