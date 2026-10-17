"""Tabelas de infraestrutura da plataforma (schema `plataforma`).

Não são tabelas de domínio: são o que substitui RabbitMQ e o object storage quando eles
não existem. Ficam aqui, e não no schema de um serviço, porque pertencem ao
mecanismo, e porque isso deixa claro que podem ser truncadas sem perder
nenhuma informação de negócio.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    Index,
    Integer,
    LargeBinary,
    SmallInteger,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from hub_core.db.base import criar_base

BasePlataforma = criar_base("plataforma")

# Estados de uma mensagem na fila.
#   pendente    -> esperando ser retirada
#   processando -> entregue a um consumidor, invisível até `invisivel_ate`
#   concluida   -> confirmada (ack)
#   morta       -> esgotou as tentativas; é a DLQ
ESTADOS_MENSAGEM = ("pendente", "processando", "concluida", "morta")


class MensagemFila(BasePlataforma):  # type: ignore[valid-type, misc]
    """Uma mensagem na fila em PostgreSQL.

    O desenho é o de uma *outbox* consumível: nada é apagado no caminho feliz,
    a mensagem muda de estado. Isso mantém o histórico do que rodou, permite
    medir tempo de espera na fila e torna o retry auditável. A limpeza é um job
    separado, com retenção explícita, em vez de um `DELETE` escondido no `ack`.
    """

    __tablename__ = "fila_mensagens"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    fila: Mapped[str] = mapped_column(String(80), nullable=False)
    corpo: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    prioridade: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="0")
    estado: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pendente")

    tentativas: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="0")
    max_tentativas: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="3")

    disponivel_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    invisivel_ate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    ultimo_erro: Mapped[str | None] = mapped_column(Text)
    trace_id: Mapped[str | None] = mapped_column(String(64))

    criado_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finalizado_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        # Índice parcial: só as pendentes interessam para a busca da próxima
        # mensagem. Em uma fila que acumula milhões de linhas concluídas, um
        # índice sobre a tabela inteira cresce sem limite e a busca degrada;
        # o parcial fica do tamanho do backlog real.
        Index(
            "ix_fila_proxima",
            "fila",
            text("prioridade DESC"),
            "disponivel_em",
            postgresql_where=text("estado = 'pendente'"),
        ),
        # Para o recuperador de expiradas.
        Index(
            "ix_fila_expiradas",
            "invisivel_ate",
            postgresql_where=text("estado = 'processando'"),
        ),
        Index("ix_fila_estado", "fila", "estado"),
        {"schema": "plataforma"},
    )


class Auditoria(BasePlataforma):  # type: ignore[valid-type, misc]
    """Trilha de auditoria. Somente inserção, gravada por todos os serviços.

    ## Por que uma tabela compartilhada entre serviços

    É a única exceção à regra de "um schema por serviço", e é consciente. As
    alternativas consideradas:

    * **um `audit-service` alimentado pela fila** (o desenho da arquitetura-alvo):
      é mais desacoplado, mas exige um consumidor sempre de pé. No modo
      monolito do free tier não há processo separado para isso, e um registro
      de auditoria que depende de um worker que pode não estar rodando é pior
      do que nenhum;
    * **uma tabela por schema**: responder "o que a Maria fez ontem" passaria a
      exigir quatro consultas e um merge em memória, para um dado que é por
      natureza um log único.

    O que torna a exceção segura é a natureza da tabela: append-only, schema
    estável, e nenhum serviço lê as linhas de outro para decidir regra de
    negócio. O acoplamento é de formato, não de comportamento.

    A escrita acontece na **mesma transação** da ação auditada. É a propriedade
    que importa: ou a execução foi disparada e o registro existe, ou nenhuma
    das duas coisas. Auditoria em transação separada perde justamente o evento
    que falhou no meio.

    ## Imutabilidade

    A migração que cria esta tabela instala `RULE`s que tornam `UPDATE` e
    `DELETE` inócuos e revoga esses privilégios do papel da aplicação. Um
    registro que o próprio sistema pode reescrever não serve como prova, e o
    único lugar onde essa garantia sobrevive a um bug na aplicação é o banco.
    """

    __tablename__ = "auditoria"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )

    # Sem FK para `identity.usuario`: a auditoria tem de sobreviver à exclusão
    # do usuário auditado, e FK entre schemas é justamente o que este projeto
    # não faz. Guarda o id e também nome e CPF como estavam no momento da ação,
    # porque "quem era essa pessoa quando isso aconteceu" é a pergunta que a
    # auditoria responde.
    ator_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    ator_nome: Mapped[str] = mapped_column(String(200), nullable=False, server_default="")
    ator_cpf: Mapped[str | None] = mapped_column(String(11))
    servico: Mapped[str] = mapped_column(String(30), nullable=False, server_default="")
    """Qual serviço gravou. Distingue "o runner finalizou a execução" de
    "o supervisor cancelou a execução"."""

    acao: Mapped[str] = mapped_column(String(80), nullable=False)
    recurso: Mapped[str] = mapped_column(String(200), nullable=False, server_default="")
    resultado: Mapped[str] = mapped_column(String(20), nullable=False, server_default="sucesso")

    ip: Mapped[str | None] = mapped_column(String(45))
    """45 caracteres: o comprimento de um IPv6 com sufixo IPv4 mapeado."""
    user_agent: Mapped[str | None] = mapped_column(String(400))
    trace_id: Mapped[str | None] = mapped_column(String(64))

    detalhes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    ocorrido_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_auditoria_ator_data", "ator_id", text("ocorrido_em DESC")),
        Index("ix_auditoria_acao_data", "acao", text("ocorrido_em DESC")),
        Index("ix_auditoria_recurso", "recurso"),
        {"schema": "plataforma"},
    )


class ArtefatoBlob(BasePlataforma):  # type: ignore[valid-type, misc]
    """Conteúdo de um artefato guardado no próprio banco.

    Substitui o object storage onde não há um. Com teto de tamanho
    (`ARTEFATO_TAMANHO_MAXIMO_BYTES`), porque `bytea` em um plano gratuito de
    0,5 GB não é lugar para arquivo grande, e porque o PostgreSQL carrega o
    valor inteiro em memória ao ler, diferente de um `GET` paginado no S3.
    """

    __tablename__ = "artefato_blob"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    caminho: Mapped[str] = mapped_column(String(500), nullable=False, unique=True)
    conteudo: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    tamanho_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tipo_conteudo: Mapped[str] = mapped_column(String(120), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    downloads: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    criado_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = ({"schema": "plataforma"},)
