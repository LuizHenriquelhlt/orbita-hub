"""Contratos de entrada e saída do `identity-service`.

São os modelos Pydantic. A separação em relação a `modelos.py` (SQLAlchemy) é
deliberada e vale o esforço: a tabela `usuario` tem `senha_hash` e
`tentativas_falhas`, e nenhum dos dois pode aparecer em resposta de API. Com um
modelo só, a cada campo novo na tabela alguém tem de lembrar de excluí-lo da
serialização — e um dia não lembra.

Aqui, o caminho é o inverso: o campo só aparece na API se estiver escrito no
esquema.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from hub_core.auth.rbac import Papel, mascarar_cpf, mascarar_email
from hub_identity import cpf as cpf_util


class PedidoDeLogin(BaseModel):
    cpf: str = Field(description="Com ou sem máscara", examples=["000.418.395-02"])
    senha: str = Field(min_length=1, max_length=200)

    @field_validator("cpf")
    @classmethod
    def _normalizar(cls, valor: str) -> str:
        digitos = cpf_util.apenas_digitos(valor)
        # Validação de formato apenas. O cálculo dos dígitos verificadores
        # fica de fora de propósito: recusar aqui um CPF bem formado mas
        # inválido entrega a diferença entre "CPF não existe no sistema" e
        # "CPF nem é um CPF", e isso é informação útil para quem enumera.
        if len(digitos) != 11:
            raise ValueError("informe os 11 dígitos do CPF")
        return digitos


class RespostaDeLogin(BaseModel):
    token: str
    tipo: str = "Bearer"
    expira_em: datetime
    usuario: UsuarioResumo


class PedidoDeLoginDemo(BaseModel):
    """Entrada no modo de demonstração: escolhe-se um papel, sem senha."""

    papel: Papel


class UnidadeResumo(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    sigla: str
    nome: str
    tipo: str
    regiao: str | None = None


class UsuarioResumo(BaseModel):
    """O usuário como o resto da plataforma o vê. Sem hash, sem contadores."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    nome: str
    email: str
    cpf: str
    matricula: str | None = None
    ativo: bool = True
    papeis: list[str] = Field(default_factory=list)
    permissoes: list[str] = Field(default_factory=list)
    unidade: UnidadeResumo | None = None
    ultimo_login: datetime | None = None

    def mascarado(self) -> UsuarioResumo:
        """Cópia com CPF e e-mail ocultos.

        Aplicado por quem **não** tem `dado_sensivel:ver`. A decisão fica na
        rota, não aqui, porque depende de quem está perguntando — e um método
        que mascara sozinho esconderia o dado também de quem tem direito a ele.
        """
        return self.model_copy(
            update={"cpf": mascarar_cpf(self.cpf), "email": mascarar_email(self.email)}
        )


class UsuarioDetalhe(UsuarioResumo):
    grupos: list[GrupoResumo] = Field(default_factory=list)
    criado_em: datetime | None = None


class GrupoResumo(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    nome: str
    tipo: str
    descricao: str | None = None
    papeis: list[str] = Field(default_factory=list)
    total_membros: int = 0


class PedidoDeCriacaoDeUsuario(BaseModel):
    cpf: str
    nome: str = Field(min_length=3, max_length=200)
    email: EmailStr
    matricula: str | None = Field(default=None, max_length=30)
    senha: str = Field(min_length=10, max_length=72)
    unidade_id: uuid.UUID | None = None
    papeis: list[Papel] = Field(default_factory=lambda: [Papel.USUARIO])

    @field_validator("cpf")
    @classmethod
    def _validar_cpf(cls, valor: str) -> str:
        # No cadastro a validação é completa: aqui não há enumeração a
        # proteger, e aceitar CPF inválido significa um registro que nunca
        # consegue entrar no sistema.
        try:
            return cpf_util.normalizar(valor)
        except ValueError as erro:
            raise ValueError("CPF inválido — confira os dígitos") from erro

    @field_validator("senha")
    @classmethod
    def _validar_senha(cls, valor: str) -> str:
        """Mínimo de 10 caracteres e três classes de caractere.

        Dez e não oito porque o NIST recomenda priorizar comprimento. Sem
        exigência de troca periódica e sem proibir caractere: as duas práticas
        reduzem a qualidade das senhas na prática, levando a `Senha@123` e a
        `Senha@124` no mês seguinte.
        """
        classes = (
            any(c.isupper() for c in valor),
            any(c.islower() for c in valor),
            any(c.isdigit() for c in valor),
            any(not c.isalnum() for c in valor),
        )
        if sum(classes) < 3:
            raise ValueError("use ao menos três entre: maiúscula, minúscula, número e símbolo")
        return valor


class PedidoDeAlteracaoDePapeis(BaseModel):
    papeis: list[Papel]


class PaginaDeUsuarios(BaseModel):
    itens: list[UsuarioResumo]
    total: int
    pagina: int
    por_pagina: int

    @property
    def total_de_paginas(self) -> int:
        return max(1, -(-self.total // self.por_pagina))


class RegistroDeAuditoria(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    ator_nome: str
    ator_cpf: str | None = None
    acao: str
    recurso: str
    resultado: str
    ip: str | None = None
    trace_id: str | None = None
    detalhes: dict = Field(default_factory=dict)
    ocorrido_em: datetime


class PaginaDeAuditoria(BaseModel):
    itens: list[RegistroDeAuditoria]
    total: int
    pagina: int
    por_pagina: int


class PapelDescrito(BaseModel):
    """Usado pela tela de ajuda e pelo seletor do modo de demonstração."""

    valor: str
    rotulo: str
    descricao: str
    permissoes: list[str]


# Resolve as referências adiante (`UsuarioResumo` citado em `RespostaDeLogin`
# antes de ser definido).
RespostaDeLogin.model_rebuild()
UsuarioDetalhe.model_rebuild()
