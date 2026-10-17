"""Papéis, permissões e a regra de decisão de acesso.

## Por que a matriz está em código

Permissão por papel é **política**, não dado operacional. Uma linha a mais na
tabela `papel_permissao` muda o comportamento de segurança do sistema sem
passar por revisão de código, sem aparecer em diff e sem deixar rastro além da
auditoria. Aqui a matriz é um `frozenset` em módulo Python: muda por commit,
aparece em *pull request*, e o teste `test_rbac.py` falha se alguém ampliar o
papel `usuario` sem perceber.

O que **é** dado e fica no banco: a quem cada papel foi concedido, em qual
escopo, por quem e quando. Isso muda todo dia e precisa ser auditável.

## As duas camadas da decisão

1. **Permissão** — "este papel pode disparar execução?" Responde a matriz
   abaixo. É RBAC puro.
2. **Escopo** — "pode disparar *esta* automação?" Depende de a automação estar
   publicada, de quem é o dono e da unidade de quem pede. É ABAC, e vive no
   `catalog-service`, que é quem conhece a automação.

Misturar as duas é o erro clássico: ou se cria um papel por automação (e a
matriz explode), ou se consulta o banco inteiro para responder "pode ver a
tela?". Separadas, a primeira camada responde em memória e a segunda só é
consultada quando a primeira passou.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum


class Papel(StrEnum):
    """Os seis papéis da plataforma, do mais amplo ao mais restrito."""

    ADMIN = "admin"
    GESTOR = "gestor"
    SUPERVISOR = "supervisor"
    OPERADOR = "operador"
    USUARIO = "usuario"
    AUDITOR = "auditor"

    @property
    def rotulo(self) -> str:
        return {
            Papel.ADMIN: "Administrador da plataforma",
            Papel.GESTOR: "Gestor",
            Papel.SUPERVISOR: "Supervisor de equipe",
            Papel.OPERADOR: "Operador",
            Papel.USUARIO: "Usuário",
            Papel.AUDITOR: "Auditor",
        }[self]

    @property
    def descricao(self) -> str:
        return {
            Papel.ADMIN: "Administra a plataforma, os runners e os acessos.",
            Papel.GESTOR: "Aprova e publica automações; acompanha indicadores da unidade.",
            Papel.SUPERVISOR: "Dispara e acompanha as execuções da própria equipe.",
            Papel.OPERADOR: "Dispara automações liberadas e acompanha o que disparou.",
            Papel.USUARIO: "Consulta o catálogo e as próprias execuções.",
            Papel.AUDITOR: "Leitura ampla, incluindo trilha de auditoria. Não dispara nada.",
        }[self]


class Permissao(StrEnum):
    """Permissões no formato `recurso:acao`."""

    AUTOMACAO_VER = "automacao:ver"
    AUTOMACAO_CRIAR = "automacao:criar"
    AUTOMACAO_EDITAR = "automacao:editar"
    AUTOMACAO_PUBLICAR = "automacao:publicar"
    AUTOMACAO_ARQUIVAR = "automacao:arquivar"

    EXECUCAO_DISPARAR = "execucao:disparar"
    EXECUCAO_VER_PROPRIAS = "execucao:ver_proprias"
    EXECUCAO_VER_TODAS = "execucao:ver_todas"
    EXECUCAO_CANCELAR = "execucao:cancelar"
    EXECUCAO_REEXECUTAR = "execucao:reexecutar"

    FILA_VER = "fila:ver"
    FILA_ADMINISTRAR = "fila:administrar"

    RUNNER_VER = "runner:ver"
    RUNNER_ADMINISTRAR = "runner:administrar"

    USUARIO_VER = "usuario:ver"
    USUARIO_GERENCIAR = "usuario:gerenciar"

    AUDITORIA_VER = "auditoria:ver"
    AUDITORIA_EXPORTAR = "auditoria:exportar"

    DASHBOARD_VER = "dashboard:ver"

    DADO_SENSIVEL_VER = "dado_sensivel:ver"
    """Vê CPF e e-mail sem máscara. Separada de propósito: ler a tela de
    usuários e ler o CPF de alguém são decisões diferentes."""


_TODAS = frozenset(Permissao)

# Leitura do catálogo e das próprias execuções: o mínimo para a plataforma ser
# útil a quem só consome.
_BASE_LEITURA = frozenset(
    {
        Permissao.AUTOMACAO_VER,
        Permissao.EXECUCAO_VER_PROPRIAS,
    }
)

PERMISSOES_POR_PAPEL: dict[Papel, frozenset[Permissao]] = {
    Papel.ADMIN: _TODAS,
    Papel.GESTOR: _BASE_LEITURA
    | {
        Permissao.AUTOMACAO_CRIAR,
        Permissao.AUTOMACAO_EDITAR,
        Permissao.AUTOMACAO_PUBLICAR,
        Permissao.AUTOMACAO_ARQUIVAR,
        Permissao.EXECUCAO_VER_TODAS,
        Permissao.EXECUCAO_DISPARAR,
        Permissao.EXECUCAO_CANCELAR,
        Permissao.FILA_VER,
        Permissao.RUNNER_VER,
        Permissao.USUARIO_VER,
        Permissao.AUDITORIA_VER,
        Permissao.DASHBOARD_VER,
    },
    Papel.SUPERVISOR: _BASE_LEITURA
    | {
        Permissao.EXECUCAO_DISPARAR,
        Permissao.EXECUCAO_VER_TODAS,
        Permissao.EXECUCAO_CANCELAR,
        Permissao.EXECUCAO_REEXECUTAR,
        Permissao.FILA_VER,
        Permissao.RUNNER_VER,
        Permissao.DASHBOARD_VER,
    },
    Papel.OPERADOR: _BASE_LEITURA
    | {
        Permissao.EXECUCAO_DISPARAR,
        Permissao.EXECUCAO_REEXECUTAR,
        Permissao.DASHBOARD_VER,
    },
    Papel.USUARIO: _BASE_LEITURA,
    # Auditor lê muito e não dispara nada. É a razão de `EXECUCAO_DISPARAR`
    # não estar aqui: quem fiscaliza não opera.
    Papel.AUDITOR: _BASE_LEITURA
    | {
        Permissao.EXECUCAO_VER_TODAS,
        Permissao.FILA_VER,
        Permissao.RUNNER_VER,
        Permissao.USUARIO_VER,
        Permissao.AUDITORIA_VER,
        Permissao.AUDITORIA_EXPORTAR,
        Permissao.DASHBOARD_VER,
        Permissao.DADO_SENSIVEL_VER,
    },
}


def permissoes_de(papeis: Iterable[str | Papel]) -> frozenset[Permissao]:
    """União das permissões dos papéis informados.

    Papel desconhecido é ignorado em silêncio, e isso é deliberado: um token
    emitido por uma versão mais nova do `identity-service` pode trazer um papel
    que esta versão do serviço não conhece. Ignorar concede menos acesso;
    levantar erro derrubaria a requisição. Em dúvida sobre segurança, falhar
    para o lado de menos permissão.
    """
    acumulado: set[Permissao] = set()
    for bruto in papeis:
        try:
            papel = Papel(bruto)
        except ValueError:
            continue
        acumulado |= PERMISSOES_POR_PAPEL[papel]
    return frozenset(acumulado)


def tem_permissao(papeis: Iterable[str | Papel], exigida: Permissao | str) -> bool:
    try:
        alvo = Permissao(exigida)
    except ValueError:
        return False
    return alvo in permissoes_de(papeis)


def mascarar_cpf(cpf: str) -> str:
    """`123.456.789-00` -> `***.456.***-**`.

    Mantém os três dígitos do meio porque é o que basta para uma pessoa
    confirmar que a linha é dela, sem reconstituir o número. Mascarar tudo
    torna a tela inútil; mostrar tudo é vazamento.
    """
    digitos = "".join(c for c in cpf if c.isdigit())
    if len(digitos) != 11:
        return "***.***.***-**"
    return f"***.{digitos[3:6]}.***-**"


def mascarar_email(email: str) -> str:
    """`maria.silva@inrh.gov.br` -> `ma***@inrh.gov.br`."""
    if "@" not in email:
        return "***"
    local, _, dominio = email.partition("@")
    visivel = local[:2] if len(local) > 2 else local[:1]
    return f"{visivel}***@{dominio}"
