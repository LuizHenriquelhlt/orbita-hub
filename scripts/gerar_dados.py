"""Gera o cadastro sintético de outorgas usado pela automação de exemplo.

    python scripts/gerar_dados.py

Determinístico: a mesma semente produz sempre o mesmo arquivo, então o
resultado da automação é reproduzível e o teste que confere os totais não
quebra a cada execução.

## Por que o arquivo é versionado no repositório

Poderia ser gerado no `make up`, e assim o repositório ficaria menor. Está
versionado por dois motivos:

* a automação de exemplo tem de funcionar em um `git clone` seguido de
  `make up`, sem passo extra e sem o visitante descobrir que falta um comando;
* e o teste que confere "1420 linhas, 8 regiões, X m³/s" precisa de um dado
  estável. Dado gerado a cada build é dado que muda, e teste sobre dado que
  muda é teste que se desliga.

## Linhas defeituosas de propósito

Cerca de 2% das linhas são inválidas: vazão vazia, situação desconhecida,
vencimento malformado, processo em branco. É deliberado — o caminho do ETL que
mais importa demonstrar é o que lida com dado ruim, e um arquivo perfeito
nunca exercita o CSV de rejeitos.
"""

from __future__ import annotations

import csv
import random
from datetime import date, timedelta
from pathlib import Path

SEMENTE = "orbita-inrh-outorgas-v1"
TOTAL_DE_LINHAS = 1420
PROPORCAO_DEFEITUOSA = 0.02

DESTINO = (
    Path(__file__).resolve().parents[1]
    / "automations"
    / "relatorio-outorgas-mensal"
    / "dados"
    / "outorgas.csv"
)

COLUNAS = [
    "processo",
    "uf",
    "regiao",
    "municipio",
    "finalidade",
    "situacao",
    "vazao_m3_s",
    "volume_anual_m3",
    "vencimento",
    "corpo_hidrico",
]

# Região -> UFs plausíveis. Coerência entre as duas colunas importa: um
# relatório de demonstração com "AM" na região Sul entrega na hora que o dado
# foi sorteado sem cuidado.
REGIOES = {
    "Norte": ("AM", "PA", "AC", "RO", "AP", "TO"),
    "Nordeste": ("BA", "PE", "CE", "MA", "PI", "RN", "PB", "AL", "SE"),
    "Centro-Oeste": ("GO", "MT", "MS", "DF"),
    "Sudeste": ("SP", "MG", "RJ", "ES"),
    "Sul": ("PR", "SC", "RS"),
    "Bacia do São Francisco": ("BA", "MG", "PE", "AL"),
    "Bacia do Paraná": ("SP", "PR", "MS", "GO"),
    "Órgão Central": ("DF",),
}

FINALIDADES = (
    "abastecimento urbano",
    "irrigação",
    "indústria",
    "mineração",
    "aquicultura",
    "termoelétrica",
    "dessedentação animal",
    "lazer",
)

# Pesos: a maioria das outorgas de um cadastro real está vigente.
SITUACOES = ("vigente", "em análise", "suspensa", "vencida")
PESOS_SITUACAO = (72, 14, 6, 8)

CORPOS_HIDRICOS = (
    "Rio Araguari",
    "Rio Formoso",
    "Ribeirão das Antas",
    "Rio Pardo Novo",
    "Córrego Santa Luzia",
    "Rio Verde Grande",
    "Açude Serra Azul",
    "Aquífero Guarani",
    "Rio Taquari-Mirim",
    "Represa do Funil Velho",
)

MUNICIPIOS = (
    "Vila Serrana",
    "Porto das Pedras Altas",
    "Campo Novo do Oeste",
    "Santa Rita da Serra",
    "Bom Retiro das Águas",
    "Alto Paraíso do Norte",
    "Riacho Fundo Velho",
    "Cachoeira do Vale Azul",
)

DEFEITOS = (
    ("vazao_m3_s", ""),
    ("situacao", "pendente de analise"),
    ("vencimento", "31/12/2026"),
    ("processo", ""),
    ("vazao_m3_s", "-4,20"),
)


def gerar() -> list[dict[str, str]]:
    aleatorio = random.Random(SEMENTE)
    base = date(2026, 1, 1)
    linhas: list[dict[str, str]] = []

    for indice in range(TOTAL_DE_LINHAS):
        regiao = aleatorio.choice(list(REGIOES))
        uf = aleatorio.choice(REGIOES[regiao])
        situacao = aleatorio.choices(SITUACOES, weights=PESOS_SITUACAO, k=1)[0]

        # Distribuição assimétrica: muitas outorgas pequenas e poucas grandes,
        # como num cadastro real. Uma uniforme daria um gráfico chapado que não
        # ensina nada sobre o dado.
        vazao = round(aleatorio.lognormvariate(0.2, 1.25), 3)
        vazao = min(max(vazao, 0.005), 180.0)

        # Vencida combina com data no passado; o resto, no futuro. Sem isso, a
        # planilha mostra outorga "vencida" com vencimento em 2028.
        if situacao == "vencida":
            vencimento = base - timedelta(days=aleatorio.randint(1, 900))
        else:
            vencimento = base + timedelta(days=aleatorio.randint(30, 2400))

        linhas.append(
            {
                "processo": f"{aleatorio.randint(10000, 99999)}."
                f"{aleatorio.randint(2015, 2026)}-{indice:05d}",
                "uf": uf,
                "regiao": regiao,
                "municipio": aleatorio.choice(MUNICIPIOS),
                "finalidade": aleatorio.choice(FINALIDADES),
                "situacao": situacao,
                "vazao_m3_s": f"{vazao:.3f}".replace(".", ","),
                "volume_anual_m3": str(int(vazao * 31_536_000)),
                "vencimento": vencimento.isoformat(),
                "corpo_hidrico": aleatorio.choice(CORPOS_HIDRICOS),
            }
        )

    # Injeta os defeitos em posições espalhadas, não nas primeiras linhas: um
    # ETL que só é testado com erro no começo do arquivo pode ter um bug que
    # só aparece depois do primeiro lote.
    quantos = int(TOTAL_DE_LINHAS * PROPORCAO_DEFEITUOSA)
    posicoes = aleatorio.sample(range(TOTAL_DE_LINHAS), quantos)
    for posicao in posicoes:
        campo, valor = aleatorio.choice(DEFEITOS)
        linhas[posicao][campo] = valor

    return linhas


def escrever(linhas: list[dict[str, str]]) -> None:
    DESTINO.parent.mkdir(parents=True, exist_ok=True)
    # Com BOM e `;`: é o que o Excel em português abre com um duplo clique,
    # sem passar pelo assistente de importação.
    with DESTINO.open("w", encoding="utf-8-sig", newline="") as arquivo:
        escritor = csv.DictWriter(arquivo, fieldnames=COLUNAS, delimiter=";", lineterminator="\r\n")
        escritor.writeheader()
        escritor.writerows(linhas)


def main() -> None:
    linhas = gerar()
    escrever(linhas)

    defeituosas = sum(
        1
        for linha in linhas
        if not linha["vazao_m3_s"]
        or not linha["processo"]
        or linha["situacao"] not in SITUACOES
        or "/" in linha["vencimento"]
        or linha["vazao_m3_s"].startswith("-")
    )
    tamanho_kb = DESTINO.stat().st_size / 1024

    print(f"{DESTINO.relative_to(Path.cwd()) if DESTINO.is_relative_to(Path.cwd()) else DESTINO}")
    print(f"  {len(linhas)} linhas, {tamanho_kb:.0f} KB")
    print(f"  {defeituosas} defeituosas de proposito ({defeituosas / len(linhas):.1%})")
    print(f"  {len(REGIOES)} regioes, {len(FINALIDADES)} finalidades")
    print(f"  semente: {SEMENTE!r} (deterministico)")


if __name__ == "__main__":
    main()
