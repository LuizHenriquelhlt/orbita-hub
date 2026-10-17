"""Middleware que coloca a identidade da sessão em `request.quem`.

## O que o portal guarda na sessão, e por quê

Duas coisas: o **token** (que vai para os serviços) e um **resumo do usuário**
(nome, papéis, permissões, unidade), usado só para montar a tela.

O resumo é cópia do que o `identity-service` devolveu no login. Guardá-lo
evita uma chamada a `/autenticacao/eu` em cada requisição só para saber se o
menu "Administração" deve aparecer.

O ponto importante, e que precisa ficar explícito: **as verificações de
permissão feitas aqui são cosméticas**. Elas escondem botão e item de menu.
A autorização de verdade acontece no serviço, que valida o JWT e aplica o
RBAC — e é o único lugar em que ela conta. Esconder o botão sem barrar o
endpoint seria segurança por obscuridade; barrar o endpoint sem esconder o
botão seria uma interface que oferece o que não funciona. Os dois existem, com
papéis diferentes.

O resumo pode ficar desatualizado: se alguém perder um papel, o menu continua
mostrando a opção até a sessão expirar. O clique então recebe 403 do serviço,
com mensagem clara. É a mesma contrapartida do JWT sem revogação imediata
(ver `hub_core.auth.tokens`), e está registrada em docs/adr/0007.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from asgiref.sync import markcoroutinefunction
from django.http import HttpRequest, HttpResponse


@dataclass(frozen=True, slots=True)
class IdentidadeDaSessao:
    """Quem está usando o portal, do ponto de vista dos templates."""

    id: str
    nome: str
    email: str
    cpf: str
    papeis: tuple[str, ...] = field(default_factory=tuple)
    permissoes: frozenset[str] = field(default_factory=frozenset)
    unidade_sigla: str = ""
    unidade_nome: str = ""
    demonstracao: bool = False

    @property
    def autenticado(self) -> bool:
        return bool(self.id)

    @property
    def primeiro_nome(self) -> str:
        return self.nome.split(" ", 1)[0] if self.nome else ""

    @property
    def iniciais(self) -> str:
        partes = [p for p in self.nome.split() if len(p) > 2]
        if not partes:
            return "?"
        return (partes[0][0] + (partes[-1][0] if len(partes) > 1 else "")).upper()

    @property
    def papel_principal(self) -> str:
        """O papel mais amplo, para exibição.

        A ordem é a de `hub_core.auth.rbac.Papel`, do mais amplo ao mais
        restrito — importada aqui para que a lista não seja duplicada.
        """
        from hub_core.auth.rbac import Papel

        for papel in Papel:
            if papel.value in self.papeis:
                return papel.rotulo
        return "Sem papel"

    def pode(self, permissao: str) -> bool:
        return permissao in self.permissoes

    # Os templates do Django não chamam método com argumento, então cada
    # permissão usada em `{% if %}` ganha uma propriedade. É verboso e é o
    # preço de não ter lógica no template — a alternativa seria uma tag
    # customizada por permissão, que é a mesma verbosidade em outro arquivo.
    @property
    def pode_disparar(self) -> bool:
        return self.pode("execucao:disparar")

    @property
    def pode_ver_todas_execucoes(self) -> bool:
        return self.pode("execucao:ver_todas")

    @property
    def pode_cancelar(self) -> bool:
        return self.pode("execucao:cancelar")

    @property
    def pode_reexecutar(self) -> bool:
        return self.pode("execucao:reexecutar")

    @property
    def pode_criar_automacao(self) -> bool:
        return self.pode("automacao:criar")

    @property
    def pode_editar_automacao(self) -> bool:
        return self.pode("automacao:editar")

    @property
    def pode_publicar(self) -> bool:
        return self.pode("automacao:publicar")

    @property
    def pode_ver_painel(self) -> bool:
        return self.pode("dashboard:ver")

    @property
    def pode_ver_usuarios(self) -> bool:
        return self.pode("usuario:ver")

    @property
    def pode_gerenciar_usuarios(self) -> bool:
        return self.pode("usuario:gerenciar")

    @property
    def pode_ver_auditoria(self) -> bool:
        return self.pode("auditoria:ver")

    @property
    def pode_ver_filas(self) -> bool:
        return self.pode("fila:ver")

    @property
    def pode_administrar_filas(self) -> bool:
        return self.pode("fila:administrar")

    @property
    def pode_ver_runners(self) -> bool:
        return self.pode("runner:ver")

    @property
    def pode_ver_dado_sensivel(self) -> bool:
        return self.pode("dado_sensivel:ver")

    @property
    def tem_area_administrativa(self) -> bool:
        """Se o menu "Administração" deve existir para esta pessoa."""
        return any(
            (
                self.pode_ver_usuarios,
                self.pode_ver_auditoria,
                self.pode_ver_filas,
                self.pode_ver_runners,
            )
        )


ANONIMO = IdentidadeDaSessao(id="", nome="", email="", cpf="")


def da_sessao(sessao: dict[str, Any]) -> IdentidadeDaSessao:
    bruto = sessao.get("usuario") or {}
    if not bruto.get("id") or not sessao.get("token"):
        return ANONIMO

    unidade = bruto.get("unidade") or {}
    return IdentidadeDaSessao(
        id=str(bruto["id"]),
        nome=bruto.get("nome", ""),
        email=bruto.get("email", ""),
        cpf=bruto.get("cpf", ""),
        papeis=tuple(bruto.get("papeis") or ()),
        permissoes=frozenset(bruto.get("permissoes") or ()),
        unidade_sigla=(unidade.get("sigla") or ""),
        unidade_nome=(unidade.get("nome") or ""),
        demonstracao=bool(bruto.get("demonstracao")),
    )


def guardar_na_sessao(sessao: Any, resposta_de_login: dict[str, Any]) -> None:
    """Guarda token e resumo. Chamado pelas views de entrada."""
    sessao["token"] = resposta_de_login["token"]
    usuario = dict(resposta_de_login.get("usuario") or {})
    usuario["demonstracao"] = resposta_de_login.get("demonstracao", False)
    sessao["usuario"] = usuario
    # `cycle_key` troca a chave da sessão depois de autenticar, invalidando a
    # anterior. É a defesa contra *session fixation*: sem isso, uma chave
    # plantada antes do login continua válida depois dele.
    sessao.cycle_key()


class IdentidadeMiddleware:
    """Expõe `request.quem` em toda requisição.

    Declarado apenas como assíncrono: o portal roda sob ASGI e as views são
    `async def`, porque é o que o transporte in-process do modo monolito exige
    (ver `nucleo.bff`). Sem `sync_capable = False`, o Django embrulharia este
    middleware em um adaptador de thread a cada requisição.

    ## `markcoroutinefunction` não é opcional

    A chamada no `__init__` parece cerimônia e é o que faz o middleware
    funcionar.

    `asyncio.iscoroutinefunction(obj)` devolve **False** para a instância de
    uma classe cujo `__call__` é `async def`: ela não é uma função corrotina,
    é um objeto chamável que devolve uma corrotina. E é exatamente essa
    verificação que o Django usa — em `convert_exception_to_response` e em
    `MiddlewareMixin.__init__` — para decidir se o elo seguinte da cadeia é
    assíncrono.

    Sem a marcação, o Django monta a cadeia achando que este middleware é
    síncrono: o middleware acima dele (por padrão o
    `XFrameOptionsMiddleware`) é instanciado em modo síncrono, chama
    `get_response(request)` sem `await`, recebe uma corrotina e tenta ler um
    cabeçalho dela. O resultado é

        AttributeError: 'coroutine' object has no attribute 'get'

    apontando para `django/middleware/clickjacking.py` — um arquivo que não
    tem nada a ver com o problema, o que torna o diagnóstico caro. Os
    middlewares do próprio Django fazem essa marcação dentro do
    `MiddlewareMixin`; quem escreve um middleware assíncrono como classe
    precisa fazê-la à mão.
    """

    async_capable = True
    sync_capable = False

    def __init__(self, get_response: Callable[[HttpRequest], Awaitable[HttpResponse]]) -> None:
        self.get_response = get_response
        markcoroutinefunction(self)

    async def __call__(self, request: HttpRequest) -> HttpResponse:
        request.quem = da_sessao(request.session)  # type: ignore[attr-defined]
        return await self.get_response(request)
