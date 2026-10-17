"""Modelo de dados do `catalog-service` (schema `catalog`).

O catálogo é a fonte da verdade sobre *o que existe e quem pode usar*. Ele não
sabe nada sobre execução, nem quantas vezes uma automação rodou, nem se a
última falhou. Esse dado vive em `execution`, e o portal compõe as duas coisas
na tela.

É uma separação que custa uma chamada HTTP a mais na ficha da automação e
compra a possibilidade de o catálogo responder quando o motor de execução está
sobrecarregado, que é exatamente quando alguém vai à plataforma procurar o que
está acontecendo.

## Onde cabe JSONB e onde não cabe

`parametros`, `limites`, `consumo_estimado` e `roteiro_simulado` são JSONB.
Todos têm a mesma característica: são lidos **sempre junto** com a automação,
nunca consultados isoladamente, e a forma deles muda com a versão do manifesto.
Normalizá-los criaria quatro tabelas que só aparecem em `JOIN` com `automacao`.

`tag` e `permissao` são tabelas. Também têm uma característica em comum: são o
critério de filtro do catálogo ("automações de cobrança", "o que esta unidade
pode executar"). Filtro quer índice, e índice em elemento de array JSONB
funciona, mas ao custo de um GIN que o planejador usa de forma menos previsível
do que um índice B-tree em coluna.

A regra que emergiu: **se é critério de busca, é coluna; se é conteúdo, é
JSONB.**
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
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
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from hub_catalog.ciclo_de_vida import StatusAutomacao
from hub_core.db.base import IdMixin, TimestampMixin, criar_base

Base = criar_base("catalog")

TIPOS_DE_AUTOMACAO = (
    "batch",
    "web",
    "api",
    "ia",
    "crawler",
    "rpa",
    "workflow",
)

CATEGORIAS = (
    "relatorios",
    "cadastro",
    "fiscalizacao",
    "integracao",
    "monitoramento",
    "documentos",
)

ESCOPOS_DE_PERMISSAO = ("usuario", "equipe", "unidade", "todos")
NIVEIS_DE_PERMISSAO = ("visualizar", "executar", "configurar")

TIPOS_DE_GATILHO = ("manual", "cron", "webhook", "api")


class Automacao(IdMixin, TimestampMixin, Base):  # type: ignore[valid-type, misc]
    """Uma automação registrada no catálogo."""

    __tablename__ = "automacao"

    slug: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    """Identificador estável e legível. É o que aparece em URL, em log e no
    manifesto, por isso não muda depois de criado, enquanto `nome` pode."""

    nome: Mapped[str] = mapped_column(String(160), nullable=False)
    descricao: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    readme: Mapped[str | None] = mapped_column(Text)

    categoria: Mapped[str] = mapped_column(String(30), nullable=False)
    tipo: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=StatusAutomacao.RASCUNHO.value
    )

    responsavel_tecnico: Mapped[str | None] = mapped_column(String(160))
    responsavel_negocio: Mapped[str | None] = mapped_column(String(160))

    # Referências lógicas para `identity`. Sem FK: schemas diferentes,
    # serviços diferentes. A integridade é garantida na escrita (o serviço
    # confere que a unidade existe) e aceita-se que uma unidade apagada deixe
    # aqui um id órfão, que é melhor do que impedir o `identity-service` de
    # apagar uma unidade por causa de uma referência em outro domínio.
    unidade_dona_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    criado_por_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    imagem: Mapped[str] = mapped_column(String(300), nullable=False, server_default="")
    """Imagem OCI (`registry/nome:tag`) ou, para automação do próprio
    repositório, `local/<slug>`."""

    entrypoint: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )

    max_execucoes_concorrentes: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default="1"
    )
    modo_visual: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    rede_habilitada: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    """Quando `false`, o container roda com a rede desligada. Padrão restritivo:
    quem precisa de rede declara, e a declaração fica visível na ficha."""

    limites: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    consumo_estimado: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    parametros: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    """Parâmetros declarados. O portal gera o formulário a partir daqui, e o
    `execution-service` valida a entrada contra isto antes de enfileirar."""

    variaveis_ambiente: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    """Apenas nome, obrigatoriedade e **referência** a segredo. Nunca valor:
    o valor é resolvido pelo runner a partir do ambiente dele."""

    roteiro_simulado: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    """Roteiro do `ExecutorSimulado`. Vazio quando a automação roda de verdade."""

    publicado_em: Mapped[date | None] = mapped_column(Date)
    depreciado_em: Mapped[date | None] = mapped_column(Date)

    versoes: Mapped[list[AutomacaoVersao]] = relationship(
        back_populates="automacao",
        cascade="all, delete-orphan",
        order_by="AutomacaoVersao.criado_em.desc()",
    )
    tags: Mapped[list[AutomacaoTag]] = relationship(
        back_populates="automacao", cascade="all, delete-orphan"
    )
    permissoes: Mapped[list[AutomacaoPermissao]] = relationship(
        back_populates="automacao", cascade="all, delete-orphan"
    )
    gatilhos: Mapped[list[Gatilho]] = relationship(
        back_populates="automacao", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint(f"tipo IN {TIPOS_DE_AUTOMACAO}", name="tipo_valido"),
        CheckConstraint(f"categoria IN {CATEGORIAS}", name="categoria_valida"),
        CheckConstraint(
            "status IN ('rascunho','homologacao','publicada','depreciada','arquivada')",
            name="status_valido",
        ),
        CheckConstraint(
            "max_execucoes_concorrentes BETWEEN 1 AND 50", name="concorrencia_plausivel"
        ),
        # Slug é a chave pública da automação e entra em URL; restringir o
        # alfabeto no banco evita que uma importação de manifesto crie um slug
        # com espaço ou acento que depois quebra o roteamento.
        CheckConstraint("slug ~ '^[a-z0-9][a-z0-9-]{1,78}[a-z0-9]$'", name="slug_bem_formado"),
        Index("ix_automacao_catalogo", "status", "categoria"),
        Index("ix_automacao_unidade", "unidade_dona_id"),
        {"schema": "catalog"},
    )

    @property
    def situacao(self) -> StatusAutomacao:
        return StatusAutomacao(self.status)

    @property
    def versao_atual(self) -> AutomacaoVersao | None:
        """A versão mais recente. `versoes` já vem ordenada por data desc."""
        return self.versoes[0] if self.versoes else None


class AutomacaoVersao(IdMixin, Base):  # type: ignore[valid-type, misc]
    """Uma versão publicada da automação.

    Existe para que uma execução registre **com qual versão** rodou. Sem isso,
    a pergunta "por que o relatório de março saiu diferente do de abril" não
    tem resposta no sistema.
    """

    __tablename__ = "automacao_versao"

    automacao_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("catalog.automacao.id", ondelete="CASCADE"),
        nullable=False,
    )
    versao: Mapped[str] = mapped_column(String(30), nullable=False)
    tag_imagem: Mapped[str] = mapped_column(String(300), nullable=False, server_default="")
    changelog: Mapped[str | None] = mapped_column(Text)

    manifesto: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    """O manifesto como foi submetido, na íntegra.

    Guardar o documento original, e não só os campos extraídos, é o que
    permite reconstituir exatamente o que foi aprovado, inclusive campos que
    uma versão futura da plataforma passe a interpretar.
    """

    criado_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    criado_por_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    automacao: Mapped[Automacao] = relationship(back_populates="versoes")

    __table_args__ = (
        UniqueConstraint("automacao_id", "versao", name="uq_versao_por_automacao"),
        # SemVer com sufixo opcional de pré-lançamento.
        CheckConstraint(r"versao ~ '^[0-9]+\.[0-9]+\.[0-9]+(-[0-9a-z.]+)?$'", name="versao_semver"),
        {"schema": "catalog"},
    )


class AutomacaoTag(Base):  # type: ignore[valid-type, misc]
    """Etiqueta livre. Tabela, e não array, porque é critério de busca."""

    __tablename__ = "automacao_tag"

    automacao_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("catalog.automacao.id", ondelete="CASCADE"),
        primary_key=True,
    )
    tag: Mapped[str] = mapped_column(String(40), primary_key=True)

    automacao: Mapped[Automacao] = relationship(back_populates="tags")

    __table_args__ = (
        CheckConstraint("tag ~ '^[a-z0-9][a-z0-9-]*$'", name="tag_bem_formada"),
        Index("ix_tag_busca", "tag"),
        {"schema": "catalog"},
    )


class AutomacaoPermissao(IdMixin, Base):  # type: ignore[valid-type, misc]
    """Quem pode usar esta automação, por escopo.

    É a camada ABAC mencionada em `hub_core.auth.rbac`: o papel diz se a pessoa
    *pode disparar automações*; esta tabela diz se ela pode disparar *esta*.

    `escopo_id` é polimórfico (aponta para usuário, grupo ou unidade conforme
    `escopo`) e aqui isso é aceitável, ao contrário de `papel_usuario` em
    `identity`: a consulta não é por `escopo_id` isolado, é sempre
    "as permissões desta automação", filtradas por `automacao_id`, que tem
    índice. O conjunto resultante é pequeno e a resolução acontece em memória.
    """

    __tablename__ = "automacao_permissao"

    automacao_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("catalog.automacao.id", ondelete="CASCADE"),
        nullable=False,
    )
    escopo: Mapped[str] = mapped_column(String(20), nullable=False)
    escopo_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    """`NULL` quando `escopo = 'todos'`."""
    nivel: Mapped[str] = mapped_column(String(20), nullable=False, server_default="executar")

    automacao: Mapped[Automacao] = relationship(back_populates="permissoes")

    __table_args__ = (
        CheckConstraint(f"escopo IN {ESCOPOS_DE_PERMISSAO}", name="escopo_valido"),
        CheckConstraint(f"nivel IN {NIVEIS_DE_PERMISSAO}", name="nivel_valido"),
        CheckConstraint(
            "(escopo = 'todos' AND escopo_id IS NULL) "
            "OR (escopo <> 'todos' AND escopo_id IS NOT NULL)",
            name="escopo_id_coerente",
        ),
        UniqueConstraint("automacao_id", "escopo", "escopo_id", "nivel", name="uq_permissao_unica"),
        Index("ix_permissao_automacao", "automacao_id"),
        {"schema": "catalog"},
    )


class Gatilho(IdMixin, TimestampMixin, Base):  # type: ignore[valid-type, misc]
    """Como a automação pode ser disparada."""

    __tablename__ = "gatilho"

    automacao_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("catalog.automacao.id", ondelete="CASCADE"),
        nullable=False,
    )
    tipo: Mapped[str] = mapped_column(String(20), nullable=False)
    expressao: Mapped[str | None] = mapped_column(String(120))
    """Expressão cron para `tipo='cron'`; caminho do webhook para `'webhook'`."""
    ativo: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    fila: Mapped[str] = mapped_column(String(60), nullable=False, server_default="execucoes.padrao")
    """Em qual fila os disparos deste gatilho entram. Um agendamento noturno
    vai para `execucoes.lote` e não concorre com o que alguém está esperando
    na tela."""

    parametros_padrao: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    automacao: Mapped[Automacao] = relationship(back_populates="gatilhos")

    __table_args__ = (
        CheckConstraint(f"tipo IN {TIPOS_DE_GATILHO}", name="tipo_valido"),
        CheckConstraint(
            "(tipo = 'cron' AND expressao IS NOT NULL) OR tipo <> 'cron'",
            name="cron_exige_expressao",
        ),
        # Um gatilho cron por automação por expressão: evita o agendamento
        # duplicado criado por reimportação de manifesto.
        UniqueConstraint("automacao_id", "tipo", "expressao", name="uq_gatilho_unico"),
        Index(
            "ix_gatilho_cron_ativo",
            "ativo",
            postgresql_where=text("tipo = 'cron' AND ativo"),
        ),
        {"schema": "catalog"},
    )
