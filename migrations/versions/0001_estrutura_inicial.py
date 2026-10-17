"""Estrutura inicial: os cinco schemas da plataforma.

Gerada por `alembic revision --autogenerate` a partir dos modelos.

Os schemas em si (`identity`, `catalog`, `execution`, `runner`,
`plataforma`) sao criados por `migrations/env.py` antes de qualquer
migracao rodar — o Alembic cria tabelas, nao schemas.

A imutabilidade de `plataforma.auditoria` vem na migracao 0002.

Revisão: 0001
Revisão anterior:
Criada em: 2026-10-17 10:48:51.950162
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "unidade",
        sa.Column("sigla", sa.String(length=20), nullable=False),
        sa.Column("nome", sa.String(length=200), nullable=False),
        sa.Column("tipo", sa.String(length=30), nullable=False),
        sa.Column("regiao", sa.String(length=60), nullable=True),
        sa.Column("ativa", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("unidade_pai_id", sa.UUID(), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "criado_em", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "atualizado_em",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "tipo IN ('diretoria', 'superintendencia', 'coordenacao', 'nucleo')",
            name=op.f("ck_unidade_tipo_valido"),
        ),
        sa.CheckConstraint(
            "unidade_pai_id IS NULL OR unidade_pai_id <> id", name=op.f("ck_unidade_sem_autopai")
        ),
        sa.ForeignKeyConstraint(
            ["unidade_pai_id"],
            ["identity.unidade.id"],
            name=op.f("fk_unidade_unidade_pai_id_unidade"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_unidade")),
        sa.UniqueConstraint("sigla", name=op.f("uq_unidade_sigla")),
        schema="identity",
    )
    op.create_table(
        "grupo",
        sa.Column("nome", sa.String(length=120), nullable=False),
        sa.Column("descricao", sa.Text(), nullable=True),
        sa.Column("tipo", sa.String(length=30), server_default="equipe", nullable=False),
        sa.Column("unidade_id", sa.UUID(), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "criado_em", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "atualizado_em",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "tipo IN ('equipe', 'coordenacao', 'divisao', 'unidade')",
            name=op.f("ck_grupo_tipo_valido"),
        ),
        sa.ForeignKeyConstraint(
            ["unidade_id"],
            ["identity.unidade.id"],
            name=op.f("fk_grupo_unidade_id_unidade"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_grupo")),
        sa.UniqueConstraint("nome", "unidade_id", name="uq_grupo_nome_unidade"),
        schema="identity",
    )
    op.create_table(
        "usuario",
        sa.Column("cpf", sa.String(length=11), nullable=False),
        sa.Column("nome", sa.String(length=200), nullable=False),
        sa.Column("email", sa.String(length=200), nullable=False),
        sa.Column("matricula", sa.String(length=30), nullable=True),
        sa.Column("senha_hash", sa.String(length=100), nullable=False),
        sa.Column("ativo", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("ultimo_login", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tentativas_falhas", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("bloqueado_ate", sa.DateTime(timezone=True), nullable=True),
        sa.Column("unidade_id", sa.UUID(), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "criado_em", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "atualizado_em",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("cpf ~ '^[0-9]{11}$'", name=op.f("ck_usuario_cpf_somente_digitos")),
        sa.CheckConstraint("position('@' in email) > 1", name=op.f("ck_usuario_email_plausivel")),
        sa.ForeignKeyConstraint(
            ["unidade_id"],
            ["identity.unidade.id"],
            name=op.f("fk_usuario_unidade_id_unidade"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_usuario")),
        sa.UniqueConstraint("cpf", name=op.f("uq_usuario_cpf")),
        sa.UniqueConstraint("email", name=op.f("uq_usuario_email")),
        schema="identity",
    )
    op.create_index(
        "ix_usuario_unidade_ativo",
        "usuario",
        ["unidade_id", "ativo"],
        unique=False,
        schema="identity",
    )
    op.create_table(
        "papel_grupo",
        sa.Column("grupo_id", sa.UUID(), nullable=False),
        sa.Column("papel", sa.String(length=30), nullable=False),
        sa.Column(
            "concedido_em",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["grupo_id"],
            ["identity.grupo.id"],
            name=op.f("fk_papel_grupo_grupo_id_grupo"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("grupo_id", "papel", name=op.f("pk_papel_grupo")),
        schema="identity",
    )
    op.create_table(
        "papel_usuario",
        sa.Column("usuario_id", sa.UUID(), nullable=False),
        sa.Column("papel", sa.String(length=30), nullable=False),
        sa.Column("concedido_por_id", sa.UUID(), nullable=True),
        sa.Column(
            "concedido_em",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["usuario_id"],
            ["identity.usuario.id"],
            name=op.f("fk_papel_usuario_usuario_id_usuario"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("usuario_id", "papel", name=op.f("pk_papel_usuario")),
        schema="identity",
    )
    op.create_table(
        "usuario_grupo",
        sa.Column("usuario_id", sa.UUID(), nullable=False),
        sa.Column("grupo_id", sa.UUID(), nullable=False),
        sa.Column(
            "vinculado_em",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["grupo_id"],
            ["identity.grupo.id"],
            name=op.f("fk_usuario_grupo_grupo_id_grupo"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["usuario_id"],
            ["identity.usuario.id"],
            name=op.f("fk_usuario_grupo_usuario_id_usuario"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("usuario_id", "grupo_id", name=op.f("pk_usuario_grupo")),
        schema="identity",
    )
    op.create_table(
        "automacao",
        sa.Column("slug", sa.String(length=80), nullable=False),
        sa.Column("nome", sa.String(length=160), nullable=False),
        sa.Column("descricao", sa.Text(), server_default="", nullable=False),
        sa.Column("readme", sa.Text(), nullable=True),
        sa.Column("categoria", sa.String(length=30), nullable=False),
        sa.Column("tipo", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="rascunho", nullable=False),
        sa.Column("responsavel_tecnico", sa.String(length=160), nullable=True),
        sa.Column("responsavel_negocio", sa.String(length=160), nullable=True),
        sa.Column("unidade_dona_id", sa.UUID(), nullable=True),
        sa.Column("criado_por_id", sa.UUID(), nullable=True),
        sa.Column("imagem", sa.String(length=300), server_default="", nullable=False),
        sa.Column(
            "entrypoint",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "max_execucoes_concorrentes", sa.SmallInteger(), server_default="1", nullable=False
        ),
        sa.Column("modo_visual", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("rede_habilitada", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "limites",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "consumo_estimado",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "parametros",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "variaveis_ambiente",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "roteiro_simulado",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("publicado_em", sa.Date(), nullable=True),
        sa.Column("depreciado_em", sa.Date(), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "criado_em", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "atualizado_em",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "categoria IN ('relatorios', 'cadastro', 'fiscalizacao', 'integracao', 'monitoramento', 'documentos')",
            name=op.f("ck_automacao_categoria_valida"),
        ),
        sa.CheckConstraint(
            "slug ~ '^[a-z0-9][a-z0-9-]{1,78}[a-z0-9]$'", name=op.f("ck_automacao_slug_bem_formado")
        ),
        sa.CheckConstraint(
            "status IN ('rascunho','homologacao','publicada','depreciada','arquivada')",
            name=op.f("ck_automacao_status_valido"),
        ),
        sa.CheckConstraint(
            "tipo IN ('batch', 'web', 'api', 'ia', 'crawler', 'rpa', 'workflow')",
            name=op.f("ck_automacao_tipo_valido"),
        ),
        sa.CheckConstraint(
            "max_execucoes_concorrentes BETWEEN 1 AND 50",
            name=op.f("ck_automacao_concorrencia_plausivel"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_automacao")),
        sa.UniqueConstraint("slug", name=op.f("uq_automacao_slug")),
        schema="catalog",
    )
    op.create_index(
        "ix_automacao_catalogo",
        "automacao",
        ["status", "categoria"],
        unique=False,
        schema="catalog",
    )
    op.create_index(
        "ix_automacao_unidade", "automacao", ["unidade_dona_id"], unique=False, schema="catalog"
    )
    op.create_table(
        "automacao_permissao",
        sa.Column("automacao_id", sa.UUID(), nullable=False),
        sa.Column("escopo", sa.String(length=20), nullable=False),
        sa.Column("escopo_id", sa.UUID(), nullable=True),
        sa.Column("nivel", sa.String(length=20), server_default="executar", nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.CheckConstraint(
            "(escopo = 'todos' AND escopo_id IS NULL) OR (escopo <> 'todos' AND escopo_id IS NOT NULL)",
            name=op.f("ck_automacao_permissao_escopo_id_coerente"),
        ),
        sa.CheckConstraint(
            "escopo IN ('usuario', 'equipe', 'unidade', 'todos')",
            name=op.f("ck_automacao_permissao_escopo_valido"),
        ),
        sa.CheckConstraint(
            "nivel IN ('visualizar', 'executar', 'configurar')",
            name=op.f("ck_automacao_permissao_nivel_valido"),
        ),
        sa.ForeignKeyConstraint(
            ["automacao_id"],
            ["catalog.automacao.id"],
            name=op.f("fk_automacao_permissao_automacao_id_automacao"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_automacao_permissao")),
        sa.UniqueConstraint(
            "automacao_id", "escopo", "escopo_id", "nivel", name="uq_permissao_unica"
        ),
        schema="catalog",
    )
    op.create_index(
        "ix_permissao_automacao",
        "automacao_permissao",
        ["automacao_id"],
        unique=False,
        schema="catalog",
    )
    op.create_table(
        "automacao_tag",
        sa.Column("automacao_id", sa.UUID(), nullable=False),
        sa.Column("tag", sa.String(length=40), nullable=False),
        sa.CheckConstraint(
            "tag ~ '^[a-z0-9][a-z0-9-]*$'", name=op.f("ck_automacao_tag_tag_bem_formada")
        ),
        sa.ForeignKeyConstraint(
            ["automacao_id"],
            ["catalog.automacao.id"],
            name=op.f("fk_automacao_tag_automacao_id_automacao"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("automacao_id", "tag", name=op.f("pk_automacao_tag")),
        schema="catalog",
    )
    op.create_index("ix_tag_busca", "automacao_tag", ["tag"], unique=False, schema="catalog")
    op.create_table(
        "automacao_versao",
        sa.Column("automacao_id", sa.UUID(), nullable=False),
        sa.Column("versao", sa.String(length=30), nullable=False),
        sa.Column("tag_imagem", sa.String(length=300), server_default="", nullable=False),
        sa.Column("changelog", sa.Text(), nullable=True),
        sa.Column(
            "manifesto",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "criado_em", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("criado_por_id", sa.UUID(), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.CheckConstraint(
            "versao ~ '^[0-9]+\\.[0-9]+\\.[0-9]+(-[0-9a-z.]+)?$'",
            name=op.f("ck_automacao_versao_versao_semver"),
        ),
        sa.ForeignKeyConstraint(
            ["automacao_id"],
            ["catalog.automacao.id"],
            name=op.f("fk_automacao_versao_automacao_id_automacao"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_automacao_versao")),
        sa.UniqueConstraint("automacao_id", "versao", name="uq_versao_por_automacao"),
        schema="catalog",
    )
    op.create_table(
        "gatilho",
        sa.Column("automacao_id", sa.UUID(), nullable=False),
        sa.Column("tipo", sa.String(length=20), nullable=False),
        sa.Column("expressao", sa.String(length=120), nullable=True),
        sa.Column("ativo", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("fila", sa.String(length=60), server_default="execucoes.padrao", nullable=False),
        sa.Column(
            "parametros_padrao",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "criado_em", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "atualizado_em",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(tipo = 'cron' AND expressao IS NOT NULL) OR tipo <> 'cron'",
            name=op.f("ck_gatilho_cron_exige_expressao"),
        ),
        sa.CheckConstraint(
            "tipo IN ('manual', 'cron', 'webhook', 'api')", name=op.f("ck_gatilho_tipo_valido")
        ),
        sa.ForeignKeyConstraint(
            ["automacao_id"],
            ["catalog.automacao.id"],
            name=op.f("fk_gatilho_automacao_id_automacao"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_gatilho")),
        sa.UniqueConstraint("automacao_id", "tipo", "expressao", name="uq_gatilho_unico"),
        schema="catalog",
    )
    op.create_index(
        "ix_gatilho_cron_ativo",
        "gatilho",
        ["ativo"],
        unique=False,
        schema="catalog",
        postgresql_where=sa.text("tipo = 'cron' AND ativo"),
    )
    op.create_table(
        "agendamento",
        sa.Column("gatilho_id", sa.UUID(), nullable=False),
        sa.Column("automacao_slug", sa.String(length=80), nullable=False),
        sa.Column("expressao", sa.String(length=120), nullable=False),
        sa.Column("fila", sa.String(length=60), server_default="execucoes.lote", nullable=False),
        sa.Column(
            "parametros_padrao",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("ultima_execucao_em", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ultima_execucao_id", sa.UUID(), nullable=True),
        sa.Column("proxima_em", sa.DateTime(timezone=True), nullable=True),
        sa.Column("disparos", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "atualizado_em",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("gatilho_id", name=op.f("pk_agendamento")),
        schema="execution",
    )
    op.create_index(
        "ix_agendamento_proxima", "agendamento", ["proxima_em"], unique=False, schema="execution"
    )
    op.create_table(
        "execucao",
        sa.Column("automacao_id", sa.UUID(), nullable=False),
        sa.Column("automacao_versao_id", sa.UUID(), nullable=True),
        sa.Column("automacao_slug", sa.String(length=80), nullable=False),
        sa.Column("automacao_nome", sa.String(length=160), server_default="", nullable=False),
        sa.Column("automacao_versao", sa.String(length=30), nullable=True),
        sa.Column("usuario_id", sa.UUID(), nullable=True),
        sa.Column("usuario_nome", sa.String(length=200), server_default="", nullable=False),
        sa.Column("usuario_cpf", sa.String(length=11), nullable=True),
        sa.Column("unidade_id", sa.UUID(), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="enfileirada", nullable=False),
        sa.Column("origem", sa.String(length=20), server_default="manual", nullable=False),
        sa.Column("fila", sa.String(length=60), server_default="execucoes.padrao", nullable=False),
        sa.Column("prioridade", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("mensagem_id", sa.UUID(), nullable=True),
        sa.Column("tentativa", sa.SmallInteger(), server_default="1", nullable=False),
        sa.Column("executor", sa.String(length=20), nullable=True),
        sa.Column(
            "enfileirada_em",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("iniciada_em", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finalizada_em", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duracao_ms", sa.Integer(), nullable=True),
        sa.Column("espera_ms", sa.Integer(), nullable=True),
        sa.Column(
            "parametros",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("resultado_resumo", sa.String(length=500), nullable=True),
        sa.Column("erro", sa.Text(), nullable=True),
        sa.Column("codigo_saida", sa.SmallInteger(), nullable=True),
        sa.Column(
            "consumo",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.CheckConstraint(
            "origem IN ('manual', 'agendada', 'webhook', 'api', 'reexecucao')",
            name=op.f("ck_execucao_origem_valida"),
        ),
        sa.CheckConstraint(
            "status IN ('enfileirada', 'executando', 'sucesso', 'falha', 'cancelada', 'timeout')",
            name=op.f("ck_execucao_status_valido"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_execucao")),
        schema="execution",
    )
    op.create_index(
        "ix_execucao_ativas",
        "execucao",
        ["status", "fila"],
        unique=False,
        schema="execution",
        postgresql_where=sa.text("status IN ('enfileirada','executando')"),
    )
    op.create_index(
        "ix_execucao_automacao_data",
        "execucao",
        ["automacao_slug", sa.literal_column("enfileirada_em DESC")],
        unique=False,
        schema="execution",
    )
    op.create_index(
        "ix_execucao_data",
        "execucao",
        [sa.literal_column("enfileirada_em DESC")],
        unique=False,
        schema="execution",
    )
    op.create_index(
        "ix_execucao_usuario_data",
        "execucao",
        ["usuario_id", sa.literal_column("enfileirada_em DESC")],
        unique=False,
        schema="execution",
    )
    op.create_table(
        "execucao_artefato",
        sa.Column("execucao_id", sa.UUID(), nullable=False),
        sa.Column("nome_arquivo", sa.String(length=260), nullable=False),
        sa.Column("caminho_storage", sa.String(length=500), nullable=False),
        sa.Column("tamanho_bytes", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column(
            "tipo_conteudo",
            sa.String(length=120),
            server_default="application/octet-stream",
            nullable=False,
        ),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column(
            "criado_em", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["execucao_id"],
            ["execution.execucao.id"],
            name=op.f("fk_execucao_artefato_execucao_id_execucao"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_execucao_artefato")),
        sa.UniqueConstraint("execucao_id", "nome_arquivo", name="uq_artefato_por_execucao"),
        schema="execution",
    )
    op.create_index(
        "ix_artefato_execucao",
        "execucao_artefato",
        ["execucao_id"],
        unique=False,
        schema="execution",
    )
    op.create_table(
        "execucao_log",
        sa.Column("execucao_id", sa.UUID(), nullable=False),
        sa.Column("sequencia", sa.Integer(), nullable=False),
        sa.Column("nivel", sa.String(length=10), server_default="INFO", nullable=False),
        sa.Column("mensagem", sa.Text(), nullable=False),
        sa.Column(
            "ocorrido_em",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "nivel IN ('DEBUG', 'INFO', 'WARNING', 'ERROR')",
            name=op.f("ck_execucao_log_nivel_valido"),
        ),
        sa.ForeignKeyConstraint(
            ["execucao_id"],
            ["execution.execucao.id"],
            name=op.f("fk_execucao_log_execucao_id_execucao"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("execucao_id", "sequencia", name=op.f("pk_execucao_log")),
        schema="execution",
    )
    op.create_table(
        "execucao_metrica",
        sa.Column("execucao_id", sa.UUID(), nullable=False),
        sa.Column("nome", sa.String(length=60), nullable=False),
        sa.Column("valor", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.ForeignKeyConstraint(
            ["execucao_id"],
            ["execution.execucao.id"],
            name=op.f("fk_execucao_metrica_execucao_id_execucao"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("execucao_id", "nome", name=op.f("pk_execucao_metrica")),
        schema="execution",
    )
    op.create_table(
        "artefato_blob",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("caminho", sa.String(length=500), nullable=False),
        sa.Column("conteudo", sa.LargeBinary(), nullable=False),
        sa.Column("tamanho_bytes", sa.BigInteger(), nullable=False),
        sa.Column("tipo_conteudo", sa.String(length=120), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("downloads", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "criado_em", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_artefato_blob")),
        sa.UniqueConstraint("caminho", name=op.f("uq_artefato_blob_caminho")),
        schema="plataforma",
    )
    op.create_table(
        "auditoria",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("ator_id", sa.UUID(), nullable=True),
        sa.Column("ator_nome", sa.String(length=200), server_default="", nullable=False),
        sa.Column("ator_cpf", sa.String(length=11), nullable=True),
        sa.Column("servico", sa.String(length=30), server_default="", nullable=False),
        sa.Column("acao", sa.String(length=80), nullable=False),
        sa.Column("recurso", sa.String(length=200), server_default="", nullable=False),
        sa.Column("resultado", sa.String(length=20), server_default="sucesso", nullable=False),
        sa.Column("ip", sa.String(length=45), nullable=True),
        sa.Column("user_agent", sa.String(length=400), nullable=True),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.Column(
            "detalhes",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "ocorrido_em",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auditoria")),
        schema="plataforma",
    )
    op.create_index(
        "ix_auditoria_acao_data",
        "auditoria",
        ["acao", sa.literal_column("ocorrido_em DESC")],
        unique=False,
        schema="plataforma",
    )
    op.create_index(
        "ix_auditoria_ator_data",
        "auditoria",
        ["ator_id", sa.literal_column("ocorrido_em DESC")],
        unique=False,
        schema="plataforma",
    )
    op.create_index(
        "ix_auditoria_recurso", "auditoria", ["recurso"], unique=False, schema="plataforma"
    )
    op.create_table(
        "fila_mensagens",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("fila", sa.String(length=80), nullable=False),
        sa.Column("corpo", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("prioridade", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("estado", sa.String(length=16), server_default="pendente", nullable=False),
        sa.Column("tentativas", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("max_tentativas", sa.SmallInteger(), server_default="3", nullable=False),
        sa.Column(
            "disponivel_em",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("invisivel_ate", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ultimo_erro", sa.Text(), nullable=True),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.Column(
            "criado_em", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("finalizado_em", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fila_mensagens")),
        schema="plataforma",
    )
    op.create_index(
        "ix_fila_estado", "fila_mensagens", ["fila", "estado"], unique=False, schema="plataforma"
    )
    op.create_index(
        "ix_fila_expiradas",
        "fila_mensagens",
        ["invisivel_ate"],
        unique=False,
        schema="plataforma",
        postgresql_where=sa.text("estado = 'processando'"),
    )
    op.create_index(
        "ix_fila_proxima",
        "fila_mensagens",
        ["fila", sa.literal_column("prioridade DESC"), "disponivel_em"],
        unique=False,
        schema="plataforma",
        postgresql_where=sa.text("estado = 'pendente'"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_fila_proxima",
        table_name="fila_mensagens",
        schema="plataforma",
        postgresql_where=sa.text("estado = 'pendente'"),
    )
    op.drop_index(
        "ix_fila_expiradas",
        table_name="fila_mensagens",
        schema="plataforma",
        postgresql_where=sa.text("estado = 'processando'"),
    )
    op.drop_index("ix_fila_estado", table_name="fila_mensagens", schema="plataforma")
    op.drop_table("fila_mensagens", schema="plataforma")
    op.drop_index("ix_auditoria_recurso", table_name="auditoria", schema="plataforma")
    op.drop_index("ix_auditoria_ator_data", table_name="auditoria", schema="plataforma")
    op.drop_index("ix_auditoria_acao_data", table_name="auditoria", schema="plataforma")
    op.drop_table("auditoria", schema="plataforma")
    op.drop_table("artefato_blob", schema="plataforma")
    op.drop_table("execucao_metrica", schema="execution")
    op.drop_table("execucao_log", schema="execution")
    op.drop_index("ix_artefato_execucao", table_name="execucao_artefato", schema="execution")
    op.drop_table("execucao_artefato", schema="execution")
    op.drop_index("ix_execucao_usuario_data", table_name="execucao", schema="execution")
    op.drop_index("ix_execucao_data", table_name="execucao", schema="execution")
    op.drop_index("ix_execucao_automacao_data", table_name="execucao", schema="execution")
    op.drop_index(
        "ix_execucao_ativas",
        table_name="execucao",
        schema="execution",
        postgresql_where=sa.text("status IN ('enfileirada','executando')"),
    )
    op.drop_table("execucao", schema="execution")
    op.drop_index("ix_agendamento_proxima", table_name="agendamento", schema="execution")
    op.drop_table("agendamento", schema="execution")
    op.drop_index(
        "ix_gatilho_cron_ativo",
        table_name="gatilho",
        schema="catalog",
        postgresql_where=sa.text("tipo = 'cron' AND ativo"),
    )
    op.drop_table("gatilho", schema="catalog")
    op.drop_table("automacao_versao", schema="catalog")
    op.drop_index("ix_tag_busca", table_name="automacao_tag", schema="catalog")
    op.drop_table("automacao_tag", schema="catalog")
    op.drop_index("ix_permissao_automacao", table_name="automacao_permissao", schema="catalog")
    op.drop_table("automacao_permissao", schema="catalog")
    op.drop_index("ix_automacao_unidade", table_name="automacao", schema="catalog")
    op.drop_index("ix_automacao_catalogo", table_name="automacao", schema="catalog")
    op.drop_table("automacao", schema="catalog")
    op.drop_table("usuario_grupo", schema="identity")
    op.drop_table("papel_usuario", schema="identity")
    op.drop_table("papel_grupo", schema="identity")
    op.drop_index("ix_usuario_unidade_ativo", table_name="usuario", schema="identity")
    op.drop_table("usuario", schema="identity")
    op.drop_table("grupo", schema="identity")
    op.drop_table("unidade", schema="identity")
