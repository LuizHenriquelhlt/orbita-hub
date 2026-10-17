"""Modelo de dados do `execution-service` (schema `execution`).

## Desnormalização deliberada

`Execucao` guarda `automacao_slug`, `automacao_nome`, `usuario_nome` e
`usuario_cpf` — dados que pertencem a `catalog` e a `identity`. Isso é cópia,
e é intencional, por dois motivos de peso diferente:

1. **Fronteira de serviço.** Não existe FK entre schemas, então a alternativa
   seria o portal fazer uma chamada ao catálogo e outra ao identity para
   montar cada linha de uma lista de 50 execuções. Guardar o rótulo troca
   duplicação de dado por autonomia de serviço.

2. **Correção histórica**, que é a razão mais forte. Uma execução é um fato
   passado. Se a automação for renomeada em 2027, o relatório de 2026 tem de
   continuar dizendo o nome que ela tinha quando rodou. Resolver o nome por
   `JOIN` no momento da leitura reescreveria o passado a cada renomeação — e
   o mesmo vale para o nome de quem disparou.

A regra que separa o certo do errado aqui: copiar **rótulo** (como a coisa se
chamava) é correto; copiar **estado** (se a automação está publicada) seria
bug, porque estado muda e a cópia envelheceria. Só rótulos e ids são copiados.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from hub_core.db.base import IdMixin, criar_base
from hub_core.executor.base import StatusExecucao

Base = criar_base("execution")

ORIGENS = ("manual", "agendada", "webhook", "api", "reexecucao")

STATUS_VALIDOS = tuple(s.value for s in StatusExecucao)

NIVEIS_DE_LOG = ("DEBUG", "INFO", "WARNING", "ERROR")


class Execucao(IdMixin, Base):  # type: ignore[valid-type, misc]
    """Uma execução de automação, do enfileiramento ao desfecho."""

    __tablename__ = "execucao"

    # ── Referências lógicas para `catalog` e `identity` ──────────────────────
    automacao_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    automacao_versao_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    automacao_slug: Mapped[str] = mapped_column(String(80), nullable=False)
    automacao_nome: Mapped[str] = mapped_column(String(160), nullable=False, server_default="")
    automacao_versao: Mapped[str | None] = mapped_column(String(30))

    usuario_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    usuario_nome: Mapped[str] = mapped_column(String(200), nullable=False, server_default="")
    usuario_cpf: Mapped[str | None] = mapped_column(String(11))
    unidade_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    # ── Estado ───────────────────────────────────────────────────────────────
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=StatusExecucao.ENFILEIRADA.value
    )
    origem: Mapped[str] = mapped_column(String(20), nullable=False, server_default="manual")
    fila: Mapped[str] = mapped_column(String(60), nullable=False, server_default="execucoes.padrao")
    prioridade: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="0")

    mensagem_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    """Id da mensagem na fila. Permite correlacionar uma execução presa com a
    mensagem que a representa, e é o que torna possível depurar a fila pela
    interface."""

    tentativa: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="1")
    executor: Mapped[str | None] = mapped_column(String(20))
    """Qual executor rodou (`simulado`, `subprocesso`, `docker`).

    Persistido porque muda o significado dos números de consumo e do próprio
    resultado. Uma execução simulada é marcada como tal na interface — ver
    `hub_core.executor.simulado`."""

    # ── Tempos ───────────────────────────────────────────────────────────────
    enfileirada_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    iniciada_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finalizada_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duracao_ms: Mapped[int | None] = mapped_column(Integer)
    espera_ms: Mapped[int | None] = mapped_column(Integer)
    """Quanto tempo ficou na fila antes de começar.

    Gravado em coluna em vez de calculado na leitura porque é a métrica que
    diz se a plataforma está com capacidade suficiente — e consulta de
    dashboard que calcula diferença de timestamp em `AVG` sobre meses de dados
    não usa índice."""

    # ── Entrada e saída ──────────────────────────────────────────────────────
    parametros: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    resultado_resumo: Mapped[str | None] = mapped_column(String(500))
    erro: Mapped[str | None] = mapped_column(Text)
    codigo_saida: Mapped[int | None] = mapped_column(SmallInteger)
    consumo: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    trace_id: Mapped[str | None] = mapped_column(String(64))

    logs: Mapped[list[ExecucaoLog]] = relationship(
        back_populates="execucao",
        cascade="all, delete-orphan",
        order_by="ExecucaoLog.sequencia",
    )
    artefatos: Mapped[list[ExecucaoArtefato]] = relationship(
        back_populates="execucao", cascade="all, delete-orphan"
    )
    metricas: Mapped[list[ExecucaoMetrica]] = relationship(
        back_populates="execucao", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint(f"status IN {STATUS_VALIDOS}", name="status_valido"),
        CheckConstraint(f"origem IN {ORIGENS}", name="origem_valida"),
        # Índices desenhados a partir das três consultas reais da interface.
        # "minhas execuções, mais recentes primeiro":
        Index("ix_execucao_usuario_data", "usuario_id", text("enfileirada_em DESC")),
        # "histórico desta automação":
        Index("ix_execucao_automacao_data", "automacao_slug", text("enfileirada_em DESC")),
        # "o que está rodando agora" — índice parcial, porque execução ativa é
        # uma fração minúscula do total e o índice fica do tamanho dela:
        Index(
            "ix_execucao_ativas",
            "status",
            "fila",
            postgresql_where=text("status IN ('enfileirada','executando')"),
        ),
        Index("ix_execucao_data", text("enfileirada_em DESC")),
        {"schema": "execution"},
    )

    @property
    def situacao(self) -> StatusExecucao:
        return StatusExecucao(self.status)

    @property
    def em_andamento(self) -> bool:
        return not self.situacao.terminal

    @property
    def simulada(self) -> bool:
        return bool((self.consumo or {}).get("simulado"))


class ExecucaoLog(Base):  # type: ignore[valid-type, misc]
    """Uma linha de log de uma execução.

    `sequencia` existe porque `ocorrido_em` não basta para ordenar: uma
    automação que emite dezenas de linhas no mesmo milissegundo produz
    timestamps empatados, e `ORDER BY ocorrido_em` devolve ordem arbitrária
    entre eles — fazendo o log aparecer embaralhado justamente no trecho mais
    movimentado, que é o que a pessoa está tentando ler.

    Chave primária composta `(execucao_id, sequencia)`: além de garantir a
    ordem, deixa as linhas de uma mesma execução fisicamente próximas no
    índice, o que é exatamente o padrão de leitura ("todo o log desta
    execução").
    """

    __tablename__ = "execucao_log"

    execucao_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("execution.execucao.id", ondelete="CASCADE"),
        primary_key=True,
    )
    sequencia: Mapped[int] = mapped_column(Integer, primary_key=True)

    nivel: Mapped[str] = mapped_column(String(10), nullable=False, server_default="INFO")
    mensagem: Mapped[str] = mapped_column(Text, nullable=False)
    ocorrido_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    execucao: Mapped[Execucao] = relationship(back_populates="logs")

    __table_args__ = (
        CheckConstraint(f"nivel IN {NIVEIS_DE_LOG}", name="nivel_valido"),
        {"schema": "execution"},
    )


class ExecucaoArtefato(IdMixin, Base):  # type: ignore[valid-type, misc]
    """Arquivo produzido por uma execução.

    A tabela guarda só os metadados; o conteúdo vive no `ArtefatoStore` (S3 ou
    `plataforma.artefato_blob`). `caminho_storage` é a chave lá.
    """

    __tablename__ = "execucao_artefato"

    execucao_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("execution.execucao.id", ondelete="CASCADE"),
        nullable=False,
    )
    nome_arquivo: Mapped[str] = mapped_column(String(260), nullable=False)
    caminho_storage: Mapped[str] = mapped_column(String(500), nullable=False)
    tamanho_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    tipo_conteudo: Mapped[str] = mapped_column(
        String(120), nullable=False, server_default="application/octet-stream"
    )
    sha256: Mapped[str | None] = mapped_column(String(64))
    criado_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    execucao: Mapped[Execucao] = relationship(back_populates="artefatos")

    __table_args__ = (
        UniqueConstraint("execucao_id", "nome_arquivo", name="uq_artefato_por_execucao"),
        Index("ix_artefato_execucao", "execucao_id"),
        {"schema": "execution"},
    )


class ExecucaoMetrica(Base):  # type: ignore[valid-type, misc]
    """Métrica que a própria automação emitiu via protocolo `@@orbita`.

    `Numeric` e não `Float`: métrica de negócio ("valor glosado", "total
    arrecadado") aparece em soma e em relatório, e ponto flutuante binário
    acumula erro de arredondamento em soma de valor monetário. O custo é não
    poder usar funções de agregação rápidas sobre float — irrelevante neste
    volume.
    """

    __tablename__ = "execucao_metrica"

    execucao_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("execution.execucao.id", ondelete="CASCADE"),
        primary_key=True,
    )
    nome: Mapped[str] = mapped_column(String(60), primary_key=True)
    valor: Mapped[float] = mapped_column(Numeric(18, 4), nullable=False)

    execucao: Mapped[Execucao] = relationship(back_populates="metricas")

    __table_args__ = ({"schema": "execution"},)


class Agendamento(Base):  # type: ignore[valid-type, misc]
    """Estado de um gatilho cron: quando rodou e quando roda de novo.

    Mora em `execution`, e não junto do gatilho em `catalog`, por uma razão de
    responsabilidade: o catálogo declara *que existe um agendamento*; o motor
    é quem sabe *o que já foi disparado*. Se este estado morasse no catálogo,
    cada disparo do agendador seria uma escrita no serviço de catálogo, que é
    predominantemente de leitura.

    `proxima_em` é materializado em coluna indexada em vez de recalculado a
    cada varredura: assim o agendador pergunta "o que vence agora" com um
    índice em vez de carregar todos os agendamentos e avaliar cada expressão
    cron em Python.
    """

    __tablename__ = "agendamento"

    gatilho_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    automacao_slug: Mapped[str] = mapped_column(String(80), nullable=False)
    expressao: Mapped[str] = mapped_column(String(120), nullable=False)
    fila: Mapped[str] = mapped_column(String(60), nullable=False, server_default="execucoes.lote")
    parametros_padrao: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    ultima_execucao_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ultima_execucao_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    proxima_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    disparos: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    atualizado_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ix_agendamento_proxima", "proxima_em"),
        {"schema": "execution"},
    )
