"""Fundação do mapeamento objeto-relacional.

Cada serviço tem o seu próprio **schema** no PostgreSQL (`identity`,
`catalog`, `execution`, `runner`). Dentro de um schema existem chaves
estrangeiras de verdade; entre schemas, não — ali a referência é lógica,
resolvida por API ou por evento.

Isso não é purismo. É o que torna a separação dos serviços verificável: se
alguém escrever um `JOIN` de `execution` para `catalog`, a migração que cria a
FK não tem o que referenciar e o erro aparece no CI, não seis meses depois na
reunião em que se decide extrair o serviço. Em troca, consultas que precisam de
dado dos dois lados ficam a cargo do BFF, que faz duas chamadas e compõe — mais
round-trips, fronteira preservada. Ver docs/adr/0003.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, MetaData, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Nomear índices e constraints explicitamente é o que faz o `alembic
# autogenerate` produzir diffs estáveis: sem isso o PostgreSQL inventa os
# nomes e cada desenvolvedor gera uma migração ligeiramente diferente.
CONVENCAO_DE_NOMES = {
    "ix": "ix_%(column_0_N_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# Quatro schemas de domínio, um por serviço, mais um de infraestrutura.
# `plataforma` guarda o que é mecanismo e não domínio: a fila do broker em
# PostgreSQL e os artefatos quando não há object storage. Fica separado para
# que ninguém confunda tabela de infraestrutura com tabela de negócio — e para
# que apagar a fila em um ambiente de teste não arraste dado de domínio.
SCHEMAS = ("identity", "catalog", "execution", "runner", "plataforma")


def criar_base(schema: str) -> type[DeclarativeBase]:
    """Devolve uma `DeclarativeBase` presa a um schema.

    Cada serviço chama isto uma vez, no seu `models.py`. O `MetaData` separado
    é o que permite rodar `metadata.create_all()` de um serviço em teste sem
    arrastar as tabelas dos outros três.
    """
    if schema not in SCHEMAS:
        raise ValueError(f"schema desconhecido: {schema!r} (esperado um de {SCHEMAS})")

    metadados = MetaData(schema=schema, naming_convention=CONVENCAO_DE_NOMES)

    class _Base(DeclarativeBase):
        metadata = metadados

        def __repr__(self) -> str:
            pk = getattr(self, "id", None)
            return f"<{type(self).__name__} id={pk}>"

    return _Base


class IdMixin:
    """Chave primária UUID gerada no banco.

    UUID em vez de serial porque o id de uma execução viaja para fora do
    sistema — vai em URL, em callback de automação, em log — e um inteiro
    sequencial nessas posições entrega volume e permite enumeração.
    `gen_random_uuid()` é nativo do PostgreSQL 13+, sem precisar de extensão.
    """

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )


class TimestampMixin:
    """`criado_em` / `atualizado_em` mantidos pelo banco, não pela aplicação.

    `server_default=now()` em vez de `default=datetime.utcnow` de propósito: o
    relógio que vale é o do banco. Com várias réplicas de serviço, relógios de
    aplicação divergem e a ordenação por data passa a mentir.
    """

    criado_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    atualizado_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
