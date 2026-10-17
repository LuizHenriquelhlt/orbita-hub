"""Dependências de autenticação e autorização para o FastAPI.

A forma de uso nas rotas:

    @router.post("/execucoes")
    async def disparar(
        quem: Identidade = Depends(exigir(Permissao.EXECUCAO_DISPARAR)),
    ): ...

`exigir` é uma fábrica de dependências: ela devolve a função que o FastAPI vai
chamar. A vantagem sobre um decorador próprio é que a exigência aparece na
assinatura — então vai para o OpenAPI, para o `/docs` e para o autocompletar do
editor, em vez de ficar escondida em um `@requires("...")` que só o runtime vê.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Annotated, Any

from fastapi import Depends, Header, Request

from hub_core.auth.rbac import Permissao
from hub_core.auth.tokens import Identidade, validar_token, validar_token_de_servico
from hub_core.errors import NaoAutenticado, SemPermissao
from hub_core.settings import Settings, obter_settings


def _extrair_bearer(cabecalho: str | None) -> str:
    if not cabecalho:
        raise NaoAutenticado("credencial ausente")
    esquema, _, valor = cabecalho.partition(" ")
    if esquema.lower() != "bearer" or not valor.strip():
        raise NaoAutenticado("esquema de autorização inválido")
    return valor.strip()


async def identidade_atual(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> Identidade:
    """Identidade do portador do token. Levanta 401 se não houver token válido."""
    settings: Settings = getattr(request.app.state, "settings", None) or obter_settings()
    identidade = validar_token(_extrair_bearer(authorization), settings)
    # Deixa a identidade no escopo da requisição para que o middleware de
    # auditoria a encontre sem repetir a validação do token.
    request.state.identidade = identidade
    return identidade


async def identidade_opcional(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> Identidade | None:
    """Como `identidade_atual`, mas devolve `None` em vez de recusar.

    Para rotas que mudam o que mostram conforme quem olha, sem exigir login.
    """
    if not authorization:
        return None
    try:
        return await identidade_atual(request, authorization)
    except NaoAutenticado:
        return None


def exigir(
    *permissoes: Permissao,
    qualquer: bool = False,
) -> Callable[..., Coroutine[Any, Any, Identidade]]:
    """Dependência que exige permissões.

    Por padrão exige **todas** as informadas. `qualquer=True` exige ao menos
    uma. O padrão é o mais restritivo porque é o que alguém espera ao ler
    `exigir(A, B)` numa rota — e porque errar para o lado restritivo gera um
    403 visível, enquanto errar para o permissivo não gera nada.
    """

    async def verificar(
        quem: Annotated[Identidade, Depends(identidade_atual)],
    ) -> Identidade:
        if not permissoes:
            return quem

        concedidas = quem.permissoes
        atende = (
            any(p in concedidas for p in permissoes)
            if qualquer
            else all(p in concedidas for p in permissoes)
        )
        if not atende:
            faltando = [p.value for p in permissoes if p not in concedidas]
            raise SemPermissao(
                "seu papel não permite esta ação",
                permissoes_faltando=faltando,
                papeis=list(quem.papeis),
            )
        return quem

    return verificar


async def chamador_interno(
    request: Request,
    x_orbita_servico: Annotated[str | None, Header()] = None,
) -> str:
    """Autentica chamada serviço-a-serviço e devolve o nome do chamador.

    Usada nos endpoints de callback — por onde o `runner-service` reporta
    progresso de execução. Esses endpoints alteram estado e não podem aceitar
    token de pessoa: um usuário com token válido não deve conseguir declarar
    que uma execução terminou com sucesso.

    Segredo compartilhado, e não o mesmo `JWT_SECRET`, exatamente para que um
    token de pessoa nunca sirva aqui.
    """
    settings: Settings = getattr(request.app.state, "settings", None) or obter_settings()
    if not x_orbita_servico:
        raise NaoAutenticado("cabeçalho X-Orbita-Servico ausente")
    return validar_token_de_servico(x_orbita_servico, settings)


# Aliases para deixar as assinaturas de rota curtas.
UsuarioAtual = Annotated[Identidade, Depends(identidade_atual)]
UsuarioOpcional = Annotated[Identidade | None, Depends(identidade_opcional)]
ServicoChamador = Annotated[str, Depends(chamador_interno)]
