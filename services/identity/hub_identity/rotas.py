"""Rotas HTTP do `identity-service`.

A camada é fina de propósito: cada rota valida entrada, chama um caso de uso de
`servico.py` e devolve um esquema. Regra de negócio aqui seria regra que o
teste de unidade não alcança sem subir um cliente HTTP.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from hub_core import auditoria as aud
from hub_core.auth import Identidade, Papel, Permissao, UsuarioAtual, emitir_token, exigir
from hub_core.auth.rbac import PERMISSOES_POR_PAPEL, permissoes_de
from hub_core.db import obter_sessao
from hub_core.errors import SemPermissao
from hub_core.settings import Settings
from hub_identity import servico
from hub_identity.esquemas import (
    GrupoResumo,
    PaginaDeAuditoria,
    PaginaDeUsuarios,
    PapelDescrito,
    PedidoDeAlteracaoDePapeis,
    PedidoDeCriacaoDeUsuario,
    PedidoDeLogin,
    PedidoDeLoginDemo,
    RegistroDeAuditoria,
    RespostaDeLogin,
    UnidadeResumo,
    UsuarioDetalhe,
    UsuarioResumo,
)

Sessao = Annotated[AsyncSession, Depends(obter_sessao)]

router = APIRouter()

autenticacao = APIRouter(prefix="/autenticacao", tags=["autenticação"])
usuarios = APIRouter(prefix="/usuarios", tags=["usuários"])
organizacao = APIRouter(tags=["organização"])
trilha = APIRouter(prefix="/auditoria", tags=["auditoria"])


def _resposta_de_login(identidade: Identidade, settings: Settings) -> RespostaDeLogin:
    token, expira_em = emitir_token(identidade, settings)
    return RespostaDeLogin(
        token=token,
        expira_em=expira_em,
        usuario=UsuarioResumo(
            id=identidade.id,
            nome=identidade.nome,
            email=identidade.email,
            cpf=identidade.cpf,
            papeis=list(identidade.papeis),
            permissoes=sorted(p.value for p in identidade.permissoes),
            unidade=None,
        ),
    )


@autenticacao.post("/login", response_model=RespostaDeLogin, summary="Entrar com CPF e senha")
async def login(
    pedido: PedidoDeLogin,
    request: Request,
    sessao: Sessao,
) -> RespostaDeLogin:
    settings: Settings = request.app.state.settings
    identidade = await servico.autenticar(
        sessao, cpf=pedido.cpf, senha=pedido.senha, requisicao=request
    )
    return _resposta_de_login(identidade, settings)


@autenticacao.post(
    "/demonstracao",
    response_model=RespostaDeLogin,
    summary="Entrar como demonstração, assumindo um papel",
)
async def login_demonstracao(
    pedido: PedidoDeLoginDemo,
    request: Request,
    sessao: Sessao,
) -> RespostaDeLogin:
    """Só responde quando `DEMO_MODE=true`.

    A verificação é feita aqui, em tempo de requisição, e não omitindo a rota
    no registro: assim o endpoint existe no OpenAPI com a explicação, e quem
    tenta usá-lo em um ambiente normal recebe um 403 que diz o motivo, em vez
    de um 404 enigmático.
    """
    settings: Settings = request.app.state.settings
    if not settings.demo_mode:
        raise SemPermissao(
            "entrada por demonstração está desligada neste ambiente",
            dica="use POST /autenticacao/login",
        )
    identidade = await servico.autenticar_demonstracao(
        sessao, papel=pedido.papel, requisicao=request
    )
    return _resposta_de_login(identidade, settings)


@autenticacao.get("/eu", response_model=UsuarioDetalhe, summary="Quem sou eu")
async def eu(quem: UsuarioAtual, sessao: Sessao) -> UsuarioDetalhe:
    return await servico.obter_usuario(sessao, quem.id)


@autenticacao.get(
    "/papeis",
    response_model=list[PapelDescrito],
    summary="Os seis papéis e o que cada um permite",
)
async def papeis() -> list[PapelDescrito]:
    """Aberta, sem autenticação.

    Alimenta o seletor de papel da demonstração e a tela de ajuda. Não vaza
    nada: a matriz de permissões está no código-fonte público do projeto.
    """
    return [
        PapelDescrito(
            valor=papel.value,
            rotulo=papel.rotulo,
            descricao=papel.descricao,
            permissoes=sorted(p.value for p in PERMISSOES_POR_PAPEL[papel]),
        )
        for papel in Papel
    ]


def _aplicar_mascara(itens: list[UsuarioResumo], quem: Identidade) -> list[UsuarioResumo]:
    """Oculta CPF e e-mail de quem não tem `dado_sensivel:ver`.

    A decisão fica na rota porque depende de quem pergunta. Um método que
    mascarasse sozinho no esquema esconderia o dado também do auditor, que tem
    direito a ele.
    """
    if quem.pode(Permissao.DADO_SENSIVEL_VER):
        return itens
    return [item.mascarado() for item in itens]


@usuarios.get("", response_model=PaginaDeUsuarios, summary="Listar usuários")
async def listar(
    sessao: Sessao,
    quem: Annotated[Identidade, Depends(exigir(Permissao.USUARIO_VER))],
    busca: Annotated[str | None, Query(max_length=120)] = None,
    unidade_id: uuid.UUID | None = None,
    papel: Annotated[str | None, Query(max_length=30)] = None,
    apenas_ativos: bool = True,
    pagina: Annotated[int, Query(ge=1)] = 1,
    por_pagina: Annotated[int, Query(ge=1, le=100)] = 25,
) -> PaginaDeUsuarios:
    itens, total = await servico.listar_usuarios(
        sessao,
        busca=busca,
        unidade_id=unidade_id,
        papel=papel,
        apenas_ativos=apenas_ativos,
        pagina=pagina,
        por_pagina=por_pagina,
    )
    return PaginaDeUsuarios(
        itens=_aplicar_mascara(itens, quem),
        total=total,
        pagina=pagina,
        por_pagina=por_pagina,
    )


@usuarios.post("", response_model=UsuarioDetalhe, status_code=201, summary="Criar usuário")
async def criar(
    pedido: PedidoDeCriacaoDeUsuario,
    request: Request,
    sessao: Sessao,
    quem: Annotated[Identidade, Depends(exigir(Permissao.USUARIO_GERENCIAR))],
) -> UsuarioDetalhe:
    _recusar_se_demonstracao(quem)
    return await servico.criar_usuario(sessao, pedido, autor=quem, requisicao=request)


@usuarios.get("/{usuario_id}", response_model=UsuarioDetalhe, summary="Detalhe do usuário")
async def detalhe(
    usuario_id: uuid.UUID,
    sessao: Sessao,
    quem: UsuarioAtual,
) -> UsuarioDetalhe:
    """Qualquer pessoa vê o próprio registro; ver o de outra exige permissão."""
    if usuario_id != quem.id and not quem.pode(Permissao.USUARIO_VER):
        raise SemPermissao("você só pode ver o seu próprio registro")

    detalhado = await servico.obter_usuario(sessao, usuario_id)
    if usuario_id != quem.id and not quem.pode(Permissao.DADO_SENSIVEL_VER):
        mascarado = detalhado.mascarado()
        return UsuarioDetalhe.model_validate(mascarado.model_dump() | {"grupos": detalhado.grupos})
    return detalhado


@usuarios.patch("/{usuario_id}/papeis", response_model=UsuarioDetalhe, summary="Substituir papéis")
async def mudar_papeis(
    usuario_id: uuid.UUID,
    pedido: PedidoDeAlteracaoDePapeis,
    request: Request,
    sessao: Sessao,
    quem: Annotated[Identidade, Depends(exigir(Permissao.USUARIO_GERENCIAR))],
) -> UsuarioDetalhe:
    _recusar_se_demonstracao(quem)
    return await servico.alterar_papeis(
        sessao, usuario_id, pedido.papeis, autor=quem, requisicao=request
    )


@usuarios.patch(
    "/{usuario_id}/situacao", response_model=UsuarioDetalhe, summary="Ativar ou desativar"
)
async def mudar_situacao(
    usuario_id: uuid.UUID,
    ativo: bool,
    request: Request,
    sessao: Sessao,
    quem: Annotated[Identidade, Depends(exigir(Permissao.USUARIO_GERENCIAR))],
) -> UsuarioDetalhe:
    _recusar_se_demonstracao(quem)
    return await servico.definir_ativo(sessao, usuario_id, ativo, autor=quem, requisicao=request)


@organizacao.get("/unidades", response_model=list[UnidadeResumo], summary="Listar unidades")
async def unidades(sessao: Sessao, quem: UsuarioAtual) -> list[UnidadeResumo]:
    del quem  # exige apenas estar autenticado
    return await servico.listar_unidades(sessao)


@organizacao.get("/grupos", response_model=list[GrupoResumo], summary="Listar grupos")
async def grupos(
    sessao: Sessao,
    quem: Annotated[Identidade, Depends(exigir(Permissao.USUARIO_VER))],
) -> list[GrupoResumo]:
    del quem
    return await servico.listar_grupos(sessao)


@trilha.get("", response_model=PaginaDeAuditoria, summary="Consultar a trilha de auditoria")
async def consultar_auditoria(
    sessao: Sessao,
    quem: Annotated[Identidade, Depends(exigir(Permissao.AUDITORIA_VER))],
    ator_id: uuid.UUID | None = None,
    acao: Annotated[str | None, Query(max_length=80)] = None,
    recurso: Annotated[str | None, Query(max_length=200)] = None,
    resultado: Annotated[str | None, Query(max_length=20)] = None,
    pagina: Annotated[int, Query(ge=1)] = 1,
    por_pagina: Annotated[int, Query(ge=1, le=200)] = 50,
) -> PaginaDeAuditoria:
    consulta = aud.consulta_de_auditoria(
        ator_id=ator_id, acao=acao, recurso=recurso, resultado=resultado
    )
    total = (await sessao.execute(aud.consulta_de_total(consulta))).scalar_one()

    paginada = consulta.offset((pagina - 1) * por_pagina).limit(por_pagina)
    registros = (await sessao.execute(paginada)).scalars().all()

    itens = [RegistroDeAuditoria.model_validate(r) for r in registros]
    if not quem.pode(Permissao.DADO_SENSIVEL_VER):
        from hub_core.auth.rbac import mascarar_cpf

        itens = [
            item.model_copy(
                update={"ator_cpf": mascarar_cpf(item.ator_cpf) if item.ator_cpf else None}
            )
            for item in itens
        ]

    return PaginaDeAuditoria(itens=itens, total=total, pagina=pagina, por_pagina=por_pagina)


def _recusar_se_demonstracao(quem: Identidade) -> None:
    """Sessão de demonstração não altera cadastro.

    O endereço público é aberto; sem esta trava, o primeiro visitante
    curioso remove o papel de administrador de todo mundo e a demonstração
    deixa de existir. Leitura e disparo de automação continuam liberados,
    é o que a demonstração precisa mostrar.
    """
    if quem.demonstracao:
        raise SemPermissao(
            "a sessão de demonstração é somente leitura para cadastros",
            dica="rode o projeto localmente para exercitar o cadastro completo",
        )


_montado = False


def montar() -> APIRouter:
    """Agrega os sub-routers. Chamado por `app.py`.

    Idempotente: no modo monolito, mais de um caminho de importação pode
    chegar aqui, e incluir o mesmo sub-router duas vezes duplica cada rota no
    OpenAPI, o FastAPI aceita sem reclamar e o `/docs` fica com tudo em
    dobro.
    """
    global _montado
    if not _montado:
        for sub in (autenticacao, usuarios, organizacao, trilha):
            router.include_router(sub)
        _montado = True
    return router


def permissoes_do_papel(papel: Papel) -> list[str]:
    return sorted(p.value for p in permissoes_de([papel]))
