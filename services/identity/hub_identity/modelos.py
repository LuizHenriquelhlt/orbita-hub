"""Modelo de dados do `identity-service` (schema `identity`).

Entidades: unidades organizacionais (hierárquicas), usuários, grupos, papéis
concedidos e a trilha de auditoria.

## Papel concedido a pessoa e a grupo

Há duas tabelas de concessão (`papel_usuario` e `papel_grupo`) em vez de uma só
com coluna polimórfica. A razão é prática: a consulta "quais papéis esta pessoa
tem" é a mais frequente do sistema — roda a cada login — e com tabelas
separadas ela é dois índices simples. Com uma tabela polimórfica, seria um
`CASE` sobre um `tipo_sujeito` que nenhum índice cobre bem.

O custo é uma tabela a mais. A alternativa custaria a consulta mais quente.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from hub_core.db.base import IdMixin, TimestampMixin, criar_base

Base = criar_base("identity")

TIPOS_DE_UNIDADE = ("diretoria", "superintendencia", "coordenacao", "nucleo")
TIPOS_DE_GRUPO = ("equipe", "coordenacao", "divisao", "unidade")


class Unidade(IdMixin, TimestampMixin, Base):  # type: ignore[valid-type, misc]
    """Unidade organizacional. Hierárquica, com auto-referência."""

    __tablename__ = "unidade"

    sigla: Mapped[str] = mapped_column(String(20), nullable=False, unique=True)
    nome: Mapped[str] = mapped_column(String(200), nullable=False)
    tipo: Mapped[str] = mapped_column(String(30), nullable=False)
    regiao: Mapped[str | None] = mapped_column(String(60))
    ativa: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))

    unidade_pai_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        # `SET NULL` e não `CASCADE`: apagar uma diretoria não deve apagar as
        # coordenações subordinadas junto com os usuários lotados nelas.
        ForeignKey("identity.unidade.id", ondelete="SET NULL"),
    )
    unidade_pai: Mapped[Unidade | None] = relationship(
        "Unidade", remote_side="Unidade.id", back_populates="filhas"
    )
    filhas: Mapped[list[Unidade]] = relationship(
        "Unidade", back_populates="unidade_pai", viewonly=True
    )

    usuarios: Mapped[list[Usuario]] = relationship(back_populates="unidade", viewonly=True)

    __table_args__ = (
        CheckConstraint(
            f"tipo IN {TIPOS_DE_UNIDADE}",
            name="tipo_valido",
        ),
        # Uma unidade não pode ser pai de si mesma. Não impede ciclo mais
        # longo (A->B->A), que exigiria uma consulta recursiva em trigger; o
        # caso de um nível é o que acontece por erro de digitação na interface.
        CheckConstraint("unidade_pai_id IS NULL OR unidade_pai_id <> id", name="sem_autopai"),
        {"schema": "identity"},
    )


class Usuario(IdMixin, TimestampMixin, Base):  # type: ignore[valid-type, misc]
    """Pessoa que usa a plataforma. Autentica por CPF.

    CPF e não e-mail porque é o identificador que o sistema de origem usa, e
    porque em órgão público uma pessoa troca de e-mail ao mudar de unidade mas
    não troca de CPF. Guardado apenas com dígitos (11 caracteres), sem máscara:
    `123.456.789-00` e `12345678900` são a mesma pessoa e, se o formato variar
    na coluna, a restrição de unicidade deixa de valer.
    """

    __tablename__ = "usuario"

    cpf: Mapped[str] = mapped_column(String(11), nullable=False, unique=True)
    nome: Mapped[str] = mapped_column(String(200), nullable=False)
    email: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    matricula: Mapped[str | None] = mapped_column(String(30))
    senha_hash: Mapped[str] = mapped_column(String(100), nullable=False)
    ativo: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    ultimo_login: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Trava temporária após tentativas erradas. Protege contra força bruta sem
    # exigir Redis: o estado mora na própria linha do usuário.
    tentativas_falhas: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="0")
    bloqueado_ate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    unidade_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("identity.unidade.id", ondelete="SET NULL")
    )
    unidade: Mapped[Unidade | None] = relationship(back_populates="usuarios")

    papeis_diretos: Mapped[list[PapelUsuario]] = relationship(
        back_populates="usuario", cascade="all, delete-orphan"
    )
    vinculos_de_grupo: Mapped[list[UsuarioGrupo]] = relationship(
        back_populates="usuario", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint("cpf ~ '^[0-9]{11}$'", name="cpf_somente_digitos"),
        CheckConstraint("position('@' in email) > 1", name="email_plausivel"),
        Index("ix_usuario_unidade_ativo", "unidade_id", "ativo"),
        {"schema": "identity"},
    )

    @property
    def esta_bloqueado(self) -> bool:
        if self.bloqueado_ate is None:
            return False
        from datetime import UTC

        return self.bloqueado_ate > datetime.now(UTC)


class Grupo(IdMixin, TimestampMixin, Base):  # type: ignore[valid-type, misc]
    """Conjunto de pessoas que recebe papéis em bloco."""

    __tablename__ = "grupo"

    nome: Mapped[str] = mapped_column(String(120), nullable=False)
    descricao: Mapped[str | None] = mapped_column(Text)
    tipo: Mapped[str] = mapped_column(String(30), nullable=False, server_default="equipe")

    unidade_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("identity.unidade.id", ondelete="CASCADE")
    )

    membros: Mapped[list[UsuarioGrupo]] = relationship(
        back_populates="grupo", cascade="all, delete-orphan"
    )
    papeis: Mapped[list[PapelGrupo]] = relationship(
        back_populates="grupo", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("nome", "unidade_id", name="uq_grupo_nome_unidade"),
        CheckConstraint(f"tipo IN {TIPOS_DE_GRUPO}", name="tipo_valido"),
        {"schema": "identity"},
    )


class UsuarioGrupo(Base):  # type: ignore[valid-type, misc]
    """Vínculo pessoa-grupo. Chave composta, sem id próprio."""

    __tablename__ = "usuario_grupo"

    usuario_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("identity.usuario.id", ondelete="CASCADE"),
        primary_key=True,
    )
    grupo_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("identity.grupo.id", ondelete="CASCADE"),
        primary_key=True,
    )
    vinculado_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    usuario: Mapped[Usuario] = relationship(back_populates="vinculos_de_grupo")
    grupo: Mapped[Grupo] = relationship(back_populates="membros")

    __table_args__ = ({"schema": "identity"},)


class PapelUsuario(Base):  # type: ignore[valid-type, misc]
    """Papel concedido diretamente a uma pessoa."""

    __tablename__ = "papel_usuario"

    usuario_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("identity.usuario.id", ondelete="CASCADE"),
        primary_key=True,
    )
    papel: Mapped[str] = mapped_column(String(30), primary_key=True)
    concedido_por_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    concedido_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    usuario: Mapped[Usuario] = relationship(back_populates="papeis_diretos")

    __table_args__ = ({"schema": "identity"},)


class PapelGrupo(Base):  # type: ignore[valid-type, misc]
    """Papel concedido a um grupo, e por herança a todos os seus membros."""

    __tablename__ = "papel_grupo"

    grupo_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("identity.grupo.id", ondelete="CASCADE"),
        primary_key=True,
    )
    papel: Mapped[str] = mapped_column(String(30), primary_key=True)
    concedido_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    grupo: Mapped[Grupo] = relationship(back_populates="papeis")

    __table_args__ = ({"schema": "identity"},)
