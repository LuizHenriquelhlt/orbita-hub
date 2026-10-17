"""Agregações do painel.

Tudo aqui é SQL de agregação, deliberadamente. A alternativa — carregar as
execuções e somar em Python — funciona com mil linhas e para de funcionar com
cem mil, e o ponto em que para não é visível em desenvolvimento.

Duas decisões que valem registro:

**`date_trunc` com `generate_series` para a série temporal.** Agrupar por dia
só devolve os dias que **têm** execução. Um gráfico de 14 dias construído com
isso mostra 9 barras e desenha uma linha contínua entre dias não adjacentes —
escondendo justamente os dias em que nada rodou, que são os que interessam.
`generate_series` cria todos os dias e o `LEFT JOIN` preenche com zero.

**Janelas em dias inteiros, não em "últimas 168 horas".** `>= current_date - 6`
dá sete dias de calendário, que é o que alguém quer dizer com "últimos 7 dias".
Uma janela deslizante de horas faz o número mudar conforme a hora da consulta,
e aí dois relatórios do mesmo dia não batem.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from hub_core.broker import FILAS, Broker
from hub_execution.agenda import ExpressaoCronInvalida, interpretar
from hub_execution.esquemas import (
    AgendamentoResumo,
    AutomacaoMaisUsada,
    IndicadorDeFila,
    Indicadores,
    SerieDiaria,
)

_SQL_CONTAGENS = text(
    """
    SELECT
        count(*)                                                      AS total,
        count(*) FILTER (WHERE status IN ('enfileirada','executando')) AS ativas,
        count(*) FILTER (WHERE enfileirada_em >= current_date)         AS hoje,
        count(*) FILTER (
            WHERE status = 'sucesso' AND enfileirada_em >= current_date - 6
        )                                                              AS sucesso_7d,
        count(*) FILTER (
            WHERE status IN ('falha','timeout')
              AND enfileirada_em >= current_date - 6
        )                                                              AS falha_7d,
        avg(duracao_ms) FILTER (WHERE status = 'sucesso')              AS duracao_media,
        avg(espera_ms)  FILTER (WHERE espera_ms IS NOT NULL)           AS espera_media
      FROM execution.execucao
    """
)

# `generate_series` garante os 14 dias; o LEFT JOIN preenche os vazios com 0.
_SQL_SERIE = text(
    """
    WITH dias AS (
        SELECT generate_series(current_date - 13, current_date, interval '1 day')::date AS dia
    )
    SELECT
        d.dia::text                                                       AS dia,
        count(e.id) FILTER (WHERE e.status = 'sucesso')                   AS sucesso,
        count(e.id) FILTER (WHERE e.status IN ('falha','timeout'))        AS falha,
        count(e.id) FILTER (
            WHERE e.status NOT IN ('sucesso','falha','timeout')
        )                                                                  AS outras
      FROM dias d
      LEFT JOIN execution.execucao e
             ON date_trunc('day', e.enfileirada_em AT TIME ZONE 'America/Sao_Paulo')::date = d.dia
     GROUP BY d.dia
     ORDER BY d.dia
    """
)

_SQL_MAIS_USADAS = text(
    """
    SELECT
        automacao_slug                                        AS slug,
        max(automacao_nome)                                   AS nome,
        count(*)                                              AS execucoes,
        count(*) FILTER (WHERE status = 'sucesso')            AS sucessos,
        avg(duracao_ms) FILTER (WHERE status = 'sucesso')     AS duracao_media
      FROM execution.execucao
     WHERE enfileirada_em >= current_date - 29
     GROUP BY automacao_slug
     ORDER BY count(*) DESC
     LIMIT 8
    """
)

_SQL_AGENDAMENTOS = text(
    """
    SELECT gatilho_id, automacao_slug, expressao, fila,
           ultima_execucao_em, proxima_em, disparos
      FROM execution.agendamento
     ORDER BY proxima_em NULLS LAST
     LIMIT 20
    """
)


async def calcular(sessao: AsyncSession, *, broker: Broker | None = None) -> Indicadores:
    linha = (await sessao.execute(_SQL_CONTAGENS)).mappings().one()

    sucesso_7d = int(linha["sucesso_7d"] or 0)
    falha_7d = int(linha["falha_7d"] or 0)
    concluidas_7d = sucesso_7d + falha_7d

    indicadores = Indicadores(
        total=int(linha["total"] or 0),
        ativas=int(linha["ativas"] or 0),
        hoje=int(linha["hoje"] or 0),
        sucesso_7d=sucesso_7d,
        falha_7d=falha_7d,
        # Dividir por zero quando nada rodou na semana é o caminho mais curto
        # para um painel que mostra erro em vez de "sem dados".
        taxa_de_sucesso_7d=(round(100 * sucesso_7d / concluidas_7d, 1) if concluidas_7d else 0.0),
        duracao_media_ms=(
            int(linha["duracao_media"]) if linha["duracao_media"] is not None else None
        ),
        espera_media_ms=(int(linha["espera_media"]) if linha["espera_media"] is not None else None),
    )

    indicadores.serie_14d = [
        SerieDiaria(
            dia=linha["dia"],
            sucesso=int(linha["sucesso"] or 0),
            falha=int(linha["falha"] or 0),
            outras=int(linha["outras"] or 0),
        )
        for linha in (await sessao.execute(_SQL_SERIE)).mappings()
    ]

    indicadores.mais_usadas = [
        AutomacaoMaisUsada(
            slug=linha["slug"],
            nome=linha["nome"] or linha["slug"],
            execucoes=int(linha["execucoes"]),
            taxa_de_sucesso=(
                round(100 * int(linha["sucessos"]) / int(linha["execucoes"]), 1)
                if linha["execucoes"]
                else 0.0
            ),
            duracao_media_ms=(
                int(linha["duracao_media"]) if linha["duracao_media"] is not None else None
            ),
        )
        for linha in (await sessao.execute(_SQL_MAIS_USADAS)).mappings()
    ]

    indicadores.agendamentos = [
        AgendamentoResumo(
            gatilho_id=linha["gatilho_id"],
            automacao_slug=linha["automacao_slug"],
            expressao=linha["expressao"],
            descricao=_descrever(linha["expressao"]),
            fila=linha["fila"],
            ultima_execucao_em=linha["ultima_execucao_em"],
            proxima_em=linha["proxima_em"],
            disparos=int(linha["disparos"] or 0),
        )
        for linha in (await sessao.execute(_SQL_AGENDAMENTOS)).mappings()
    ]

    if broker is not None:
        for fila in FILAS:
            try:
                profundidade = await broker.profundidade(fila)
            except Exception:
                profundidade = {}
            indicadores.filas.append(
                IndicadorDeFila(
                    fila=fila,
                    pendente=profundidade.get("pendente", 0),
                    processando=profundidade.get("processando", 0),
                    concluida=profundidade.get("concluida", 0),
                    morta=profundidade.get("morta", 0),
                )
            )

    return indicadores


def _descrever(expressao: str) -> str:
    try:
        return interpretar(expressao).descrever()
    except ExpressaoCronInvalida:
        return expressao


async def resumo_por_automacao(sessao: AsyncSession, slug: str) -> dict[str, Any]:
    """Estatísticas de uma automação, para a ficha dela no catálogo.

    Consumido pelo portal, que compõe isto com os dados do `catalog-service` —
    é o lugar onde a separação dos dois serviços cobra a sua chamada extra.
    """
    consulta = text(
        """
        SELECT
            count(*)                                           AS total,
            count(*) FILTER (WHERE status = 'sucesso')         AS sucessos,
            count(*) FILTER (WHERE status IN ('falha','timeout')) AS falhas,
            count(*) FILTER (WHERE status IN ('enfileirada','executando')) AS ativas,
            avg(duracao_ms) FILTER (WHERE status = 'sucesso')  AS duracao_media,
            max(enfileirada_em)                                AS ultima_em
          FROM execution.execucao
         WHERE automacao_slug = :slug
        """
    )
    linha = (await sessao.execute(consulta, {"slug": slug})).mappings().one()
    total = int(linha["total"] or 0)
    concluidas = int(linha["sucessos"] or 0) + int(linha["falhas"] or 0)
    return {
        "total": total,
        "sucessos": int(linha["sucessos"] or 0),
        "falhas": int(linha["falhas"] or 0),
        "ativas": int(linha["ativas"] or 0),
        "taxa_de_sucesso": (
            round(100 * int(linha["sucessos"] or 0) / concluidas, 1) if concluidas else None
        ),
        "duracao_media_ms": (
            int(linha["duracao_media"]) if linha["duracao_media"] is not None else None
        ),
        "ultima_em": linha["ultima_em"],
    }
