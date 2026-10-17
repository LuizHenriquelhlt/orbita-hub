"""Rotas HTTP do `catalog-service`."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from hub_catalog import servico
from hub_catalog.ciclo_de_vida import StatusAutomacao
from hub_catalog.esquemas import (
    AutomacaoDetalhe,
    ChecagemDeAcesso,
    PaginaDeAutomacoes,
    PedidoDeAtualizacao,
    PedidoDeCriacao,
    PedidoDeImportacaoDeManifesto,
    PedidoDeNovaVersao,
    PedidoDePreparacao,
    PedidoDeTransicao,
    ResultadoDeImportacao,
)
from hub_catalog.manifesto import gerar_json_schema
from hub_core.auth import Identidade, Permissao, UsuarioAtual, exigir
from hub_core.db import obter_sessao
from hub_core.errors import SemPermissao

Sessao = Annotated[AsyncSession, Depends(obter_sessao)]

router = APIRouter()
automacoes = APIRouter(prefix="/automacoes", tags=["automações"])
manifestos = APIRouter(prefix="/manifestos", tags=["manifestos"])


@automacoes.get("", response_model=PaginaDeAutomacoes, summary="Catálogo de automações")
async def listar(
    sessao: Sessao,
    quem: Annotated[Identidade, Depends(exigir(Permissao.AUTOMACAO_VER))],
    busca: Annotated[str | None, Query(max_length=120)] = None,
    categoria: Annotated[str | None, Query(max_length=30)] = None,
    tipo: Annotated[str | None, Query(max_length=20)] = None,
    tag: Annotated[str | None, Query(max_length=40)] = None,
    status: Annotated[str | None, Query(max_length=20)] = None,
    pagina: Annotated[int, Query(ge=1)] = 1,
    por_pagina: Annotated[int, Query(ge=1, le=100)] = 24,
) -> PaginaDeAutomacoes:
    itens, total, facetas = await servico.listar(
        sessao,
        quem,
        busca=busca,
        categoria=categoria,
        tipo=tipo,
        tag=tag,
        status=status,
        pagina=pagina,
        por_pagina=por_pagina,
    )
    return PaginaDeAutomacoes(
        itens=itens, total=total, pagina=pagina, por_pagina=por_pagina, facetas=facetas
    )


@automacoes.post(
    "", response_model=AutomacaoDetalhe, status_code=201, summary="Cadastrar automação"
)
async def criar(
    pedido: PedidoDeCriacao,
    request: Request,
    sessao: Sessao,
    quem: Annotated[Identidade, Depends(exigir(Permissao.AUTOMACAO_CRIAR))],
) -> AutomacaoDetalhe:
    _recusar_se_demonstracao(quem)
    return await servico.criar(sessao, pedido, autor=quem, requisicao=request)


@automacoes.get("/{slug}", response_model=AutomacaoDetalhe, summary="Ficha da automação")
async def detalhe(
    slug: str,
    sessao: Sessao,
    quem: Annotated[Identidade, Depends(exigir(Permissao.AUTOMACAO_VER))],
) -> AutomacaoDetalhe:
    return await servico.obter(sessao, slug, quem)


@automacoes.patch("/{slug}", response_model=AutomacaoDetalhe, summary="Editar automação")
async def editar(
    slug: str,
    pedido: PedidoDeAtualizacao,
    request: Request,
    sessao: Sessao,
    quem: Annotated[Identidade, Depends(exigir(Permissao.AUTOMACAO_EDITAR))],
) -> AutomacaoDetalhe:
    _recusar_se_demonstracao(quem)
    return await servico.atualizar(sessao, slug, pedido, autor=quem, requisicao=request)


@automacoes.post(
    "/{slug}/transicao",
    response_model=AutomacaoDetalhe,
    summary="Mover a automação no ciclo de vida",
)
async def transicionar(
    slug: str,
    pedido: PedidoDeTransicao,
    request: Request,
    sessao: Sessao,
    quem: UsuarioAtual,
) -> AutomacaoDetalhe:
    """A permissão exigida depende da transição pedida.

    Por isso não há `Depends(exigir(...))` aqui: enviar para homologação pede
    `automacao:editar`, publicar pede `automacao:publicar` e arquivar pede
    `automacao:arquivar`. A verificação acontece em `servico.transicionar`, que
    é quem consulta a tabela de transições — ter a exigência em dois lugares
    seria ter duas respostas possíveis para a mesma pergunta.
    """
    _recusar_se_demonstracao(quem)
    return await servico.transicionar(
        sessao,
        slug,
        StatusAutomacao(pedido.para),
        autor=quem,
        justificativa=pedido.justificativa,
        requisicao=request,
    )


@automacoes.post(
    "/{slug}/versoes",
    response_model=AutomacaoDetalhe,
    status_code=201,
    summary="Registrar nova versão",
)
async def nova_versao(
    slug: str,
    pedido: PedidoDeNovaVersao,
    request: Request,
    sessao: Sessao,
    quem: Annotated[Identidade, Depends(exigir(Permissao.AUTOMACAO_EDITAR))],
) -> AutomacaoDetalhe:
    _recusar_se_demonstracao(quem)
    return await servico.registrar_versao(sessao, slug, pedido, autor=quem, requisicao=request)


@automacoes.post(
    "/{slug}/preparar-execucao",
    response_model=ChecagemDeAcesso,
    summary="Verificar acesso e validar parâmetros (chamado pelo execution-service)",
)
async def preparar_execucao(
    slug: str,
    pedido: PedidoDePreparacao,
    sessao: Sessao,
    quem: UsuarioAtual,
) -> ChecagemDeAcesso:
    """Autentica com o **token da pessoa**, não com token de serviço.

    O `execution-service` repassa o token de quem pediu a execução, em vez de
    usar credencial própria. Isso mantém a decisão de acesso baseada em quem
    de fato está disparando — e impede que um motor comprometido autorize
    qualquer execução em nome de qualquer um.
    """
    return await servico.preparar_execucao(
        sessao, slug, quem, parametros=pedido.parametros, fila=pedido.fila
    )


@automacoes.post("/{slug}/permissoes", response_model=AutomacaoDetalhe, summary="Conceder acesso")
async def conceder(
    slug: str,
    request: Request,
    sessao: Sessao,
    quem: Annotated[Identidade, Depends(exigir(Permissao.AUTOMACAO_EDITAR))],
    escopo: Annotated[str, Query(pattern="^(usuario|equipe|unidade|todos)$")] = "todos",
    nivel: Annotated[str, Query(pattern="^(visualizar|executar|configurar)$")] = "executar",
    escopo_id: uuid.UUID | None = None,
) -> AutomacaoDetalhe:
    _recusar_se_demonstracao(quem)
    return await servico.conceder_permissao(
        sessao,
        slug,
        escopo=escopo,
        escopo_id=escopo_id,
        nivel=nivel,
        autor=quem,
        requisicao=request,
    )


@automacoes.get(
    "/_internos/gatilhos-cron",
    summary="Agendamentos ativos (chamado pelo agendador)",
    include_in_schema=False,
)
async def gatilhos_cron(
    sessao: Sessao,
    quem: Annotated[Identidade, Depends(exigir(Permissao.AUTOMACAO_VER))],
) -> list[dict[str, Any]]:
    del quem
    return await servico.listar_gatilhos_cron(sessao)


@manifestos.post(
    "/importar",
    response_model=ResultadoDeImportacao,
    summary="Importar um automation.yaml",
)
async def importar(
    pedido: PedidoDeImportacaoDeManifesto,
    request: Request,
    sessao: Sessao,
    quem: Annotated[Identidade, Depends(exigir(Permissao.AUTOMACAO_CRIAR))],
) -> ResultadoDeImportacao:
    _recusar_se_demonstracao(quem)
    resolver = _resolvedor_de_unidade(request)
    return await servico.importar_manifesto(
        sessao,
        pedido.manifesto_yaml,
        autor=quem,
        publicar=pedido.publicar,
        resolver_unidade=resolver,
        requisicao=request,
    )


@manifestos.get("/schema", summary="JSON Schema do manifesto de automação")
async def schema() -> dict[str, Any]:
    """Aberta, sem autenticação.

    É um contrato público: quem escreve uma automação aponta o editor para
    esta URL e ganha autocompletar. Exigir token aqui significaria que a
    validação no editor só funciona para quem já tem credencial da
    plataforma.
    """
    return gerar_json_schema()


def _resolvedor_de_unidade(request: Request) -> Any:
    """Devolve uma função que traduz sigla de unidade em id, via `identity`.

    Injetada em vez de importada porque é a única coisa de que o catálogo
    precisa do `identity-service`. Passar a função como parâmetro mantém
    `servico.py` testável sem cliente HTTP — o teste passa um `dict.get`.
    """
    cliente = getattr(request.app.state, "cliente_identity", None)
    if cliente is None:
        return None

    token = request.headers.get("authorization", "").removeprefix("Bearer ").strip()

    async def resolver(sigla: str) -> uuid.UUID | None:
        try:
            unidades = await cliente.get("/unidades", token=token)
        except Exception:
            return None
        for unidade in unidades or []:
            if str(unidade.get("sigla", "")).upper() == sigla.upper():
                return uuid.UUID(unidade["id"])
        return None

    return resolver


def _recusar_se_demonstracao(quem: Identidade) -> None:
    if quem.demonstracao:
        raise SemPermissao(
            "a sessão de demonstração não altera o catálogo",
            dica="disparar execução continua liberado",
        )


_montado = False


def montar() -> APIRouter:
    global _montado
    if not _montado:
        router.include_router(automacoes)
        router.include_router(manifestos)
        _montado = True
    return router
