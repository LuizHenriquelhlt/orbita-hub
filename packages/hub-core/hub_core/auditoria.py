"""Gravação da trilha de auditoria.

Duas formas de uso, para dois momentos diferentes:

**Explícita**, quando a ação *é* o que se quer auditar e a sessão está em mão:

    await registrar(
        sessao, acao="execucao.disparada", recurso=str(execucao.id),
        ator=quem, requisicao=request, detalhes={"automacao": slug},
    )

**Por evento**, para que um serviço não precise saber que a auditoria existe:
`assinar_eventos()` liga o barramento de `hub_core.events` a esta tabela.

A gravação explícita é a preferida nas ações sensíveis (login, mudança de
papel, publicação de automação), porque participa da transação do chamador — e
porque deixa o registro de auditoria visível no mesmo trecho de código que a
ação, onde quem revisa o consegue conferir.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from hub_core.auth.tokens import Identidade
from hub_core.db.plataforma import Auditoria
from hub_core.events import EventoDeDominio, barramento
from hub_core.logging import obter_logger, obter_trace_id

log = obter_logger(__name__)

SUCESSO = "sucesso"
FALHA = "falha"
RECUSADO = "recusado"


def _ip_de(requisicao: Any) -> str | None:
    """IP do cliente, respeitando o proxy à frente.

    Render, Cloud Run e qualquer load balancer põem o IP real em
    `X-Forwarded-For` e o seu próprio em `request.client.host`. Sem ler o
    cabeçalho, toda linha de auditoria registraria o IP do balanceador.

    Pega o **primeiro** da lista: é o cliente original. Os seguintes são os
    proxies da cadeia. Vale notar que o cabeçalho é falsificável por quem fala
    direto com a aplicação — atrás de um balanceador que o reescreve, como é o
    caso aqui, ele é confiável; exposto direto na internet, não seria.
    """
    if requisicao is None:
        return None
    encaminhado = requisicao.headers.get("x-forwarded-for")
    if encaminhado:
        return encaminhado.split(",")[0].strip()[:45]
    cliente = getattr(requisicao, "client", None)
    return getattr(cliente, "host", None)


async def registrar(
    sessao: AsyncSession,
    *,
    acao: str,
    recurso: str = "",
    resultado: str = SUCESSO,
    ator: Identidade | None = None,
    ator_nome: str = "",
    ator_cpf: str | None = None,
    ator_id: uuid.UUID | None = None,
    servico: str = "",
    requisicao: Any = None,
    detalhes: dict[str, Any] | None = None,
) -> None:
    """Insere um registro na trilha. Não dá commit — quem chama controla a transação.

    Essa é a decisão central do módulo: sem `commit` aqui, o registro entra na
    transação da ação. Se a ação for desfeita, o registro também. Se a ação
    persistir, o registro persistiu com ela. Não existe o caso "ação feita,
    auditoria perdida".
    """
    sessao.add(
        Auditoria(
            ator_id=ator.id if ator else ator_id,
            ator_nome=(ator.nome if ator else ator_nome)[:200],
            ator_cpf=(ator.cpf if ator else ator_cpf) or None,
            servico=servico[:30],
            acao=acao[:80],
            recurso=recurso[:200],
            resultado=resultado[:20],
            ip=_ip_de(requisicao),
            user_agent=(requisicao.headers.get("user-agent", "")[:400] if requisicao else None),
            trace_id=obter_trace_id(),
            detalhes=_sanear(detalhes or {}),
        )
    )


def _sanear(detalhes: dict[str, Any]) -> dict[str, Any]:
    """Remove de `detalhes` o que nunca deve ir para a auditoria.

    A trilha é lida por mais gente do que o banco de produção — o papel
    `auditor` tem acesso a ela por desenho. Senha, token e segredo em
    `detalhes` seriam vazamento para dentro do próprio mecanismo de controle,
    e o jeito de impedir isso não é confiar em quem chama: é filtrar aqui.
    """
    proibidos = ("senha", "password", "token", "secret", "segredo", "authorization", "api_key")
    limpo: dict[str, Any] = {}
    for chave, valor in detalhes.items():
        if any(p in chave.lower() for p in proibidos):
            limpo[chave] = "[removido]"
        elif isinstance(valor, dict):
            limpo[chave] = _sanear(valor)
        elif isinstance(valor, str | int | float | bool | type(None)):
            limpo[chave] = valor
        elif isinstance(valor, list):
            limpo[chave] = [v for v in valor if isinstance(v, str | int | float | bool)][:50]
        else:
            limpo[chave] = str(valor)[:500]
    return limpo


def assinar_eventos(fabrica_de_sessao: Any, *, servico: str) -> None:
    """Liga o barramento de eventos à trilha de auditoria.

    Este caminho **dá commit**, porque o assinante roda fora da transação do
    emissor. É o caminho de menor garantia dos dois, e por isso é usado só para
    evento informativo; ação sensível chama `registrar` diretamente.
    """

    async def gravar(evento: EventoDeDominio) -> None:
        async with fabrica_de_sessao() as sessao:
            await registrar(
                sessao,
                acao=evento.nome,
                recurso=evento.recurso,
                ator_id=evento.ator_id,
                ator_nome=evento.ator_nome,
                servico=servico,
                detalhes=evento.dados,
            )
            await sessao.commit()

    barramento.assinar("*", gravar)
    log.info("auditoria.assinada", servico=servico)


def consulta_de_auditoria(
    *,
    ator_id: uuid.UUID | None = None,
    acao: str | None = None,
    recurso: str | None = None,
    resultado: str | None = None,
) -> Select[tuple[Auditoria]]:
    """Monta a consulta filtrada, ordenada do mais recente para o mais antigo."""
    consulta = select(Auditoria).order_by(Auditoria.ocorrido_em.desc())
    if ator_id is not None:
        consulta = consulta.where(Auditoria.ator_id == ator_id)
    if acao:
        consulta = consulta.where(Auditoria.acao.ilike(f"{acao}%"))
    if recurso:
        consulta = consulta.where(Auditoria.recurso == recurso)
    if resultado:
        consulta = consulta.where(Auditoria.resultado == resultado)
    return consulta


def consulta_de_total(consulta: Select[tuple[Auditoria]]) -> Select[tuple[int]]:
    """Total de linhas da consulta, para a paginação.

    `order_by(None)` antes de contar: ordenar um `COUNT(*)` é trabalho puro
    jogado fora, e o PostgreSQL não otimiza isso sozinho dentro da subconsulta.
    """
    return select(func.count()).select_from(consulta.order_by(None).subquery())
