"""Configuração única da plataforma, lida do ambiente.

Um objeto `Settings` serve os quatro serviços. É uma escolha consciente: em um
monorepo com um único artefato de deploy, manter quatro arquivos de
configuração que precisam concordar entre si é pior do que um só com campos que
alguns serviços ignoram.

Os quatro campos `*_backend` são o coração do desenho: trocar `rabbitmq` por
`postgres` em `BROKER_BACKEND` troca a implementação da fila sem tocar em uma
linha de regra de negócio.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ModoHub = Literal["monolith", "distributed"]
BackendFila = Literal["postgres", "rabbitmq"]
BackendExecutor = Literal["simulated", "subprocess", "docker"]
BackendStorage = Literal["postgres", "s3"]
BackendLock = Literal["postgres", "redis"]

# Parâmetros de conexão que são sintaxe de libpq e que o asyncpg não conhece.
# Chegam colados na URL que Neon, Supabase e Render entregam.
_PARAMS_LIBPQ = frozenset(
    {"sslmode", "channel_binding", "options", "target_session_attrs", "connect_timeout"}
)


def normalizar_database_url(url: str) -> str:
    """Põe a URL do banco no formato que SQLAlchemy + asyncpg esperam.

    Três coisas dão errado na prática, e é mais barato resolvê-las aqui, uma
    vez, do que descobrir cada uma em produção:

    1. Provedores gerenciados entregam a URL como ``postgres://`` ou
       ``postgresql://``. Sem o sufixo ``+asyncpg`` o SQLAlchemy escolhe o
       driver síncrono e o processo bloqueia o event loop no primeiro acesso.
    2. Essas URLs vêm com ``?sslmode=require``, que é sintaxe de libpq. O
       asyncpg não conhece o parâmetro e levanta ``TypeError`` na conexão. O
       equivalente dele é ``ssl=...`` em ``connect_args``, montado em
       `hub_core.db.engine`; aqui o parâmetro é apenas retirado da query.
    3. ``channel_binding`` e ``options`` quebram do mesmo jeito, e o Neon
       inclui o primeiro por padrão.
    """
    partes = urlsplit(url)

    esquema = partes.scheme
    if esquema in {"postgres", "postgresql", "postgresql+psycopg2", "postgresql+psycopg"}:
        esquema = "postgresql+asyncpg"

    query = urlencode([(c, v) for c, v in parse_qsl(partes.query) if c not in _PARAMS_LIBPQ])

    return urlunsplit((esquema, partes.netloc, partes.path, query, partes.fragment))


def url_exige_ssl(url: str) -> bool:
    """Diz se a URL original pedia TLS, antes de `normalizar_database_url` limpá-la."""
    valores = dict(parse_qsl(urlsplit(url).query))
    return valores.get("sslmode") in {"require", "verify-ca", "verify-full"}


def url_usa_pooler(url: str) -> bool:
    """Detecta o endpoint *pooled* do Neon (PgBouncer em modo transação).

    Importa porque o asyncpg usa *prepared statements* nomeados por padrão, e o
    PgBouncer em modo transação não os suporta: a segunda query da conexão
    falha com ``prepared statement "__asyncpg_stmt_1__" does not exist``. A
    correção é desligar o cache de statements, feito em `hub_core.db.engine`.
    """
    host = urlsplit(url).hostname or ""
    return "-pooler." in host


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── Composição ───────────────────────────────────────────────────────────
    hub_mode: ModoHub = "monolith"
    """Como os serviços conversam entre si. Ver `hub_core.clients`."""

    ambiente: Literal["local", "demo", "producao"] = "local"

    # ── Banco ────────────────────────────────────────────────────────────────
    database_url: str = "postgresql+asyncpg://orbita:orbita_dev@localhost:5432/orbita"
    db_echo: bool = False
    db_pool_size: int = 5
    db_max_overflow: int = 5

    # Guardado antes da normalização, para decidir TLS e cache de statements.
    database_url_original: str = ""

    # ── Adaptadores plugáveis ────────────────────────────────────────────────
    broker_backend: BackendFila = "postgres"
    executor_backend: BackendExecutor = "simulated"
    storage_backend: BackendStorage = "postgres"
    lock_backend: BackendLock = "postgres"

    rabbitmq_url: str = "amqp://orbita:orbita_dev@localhost:5672/"
    redis_url: str = "redis://localhost:6379/0"

    s3_endpoint_url: str | None = None
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_bucket: str = "orbita-artefatos"
    s3_region: str = "us-east-1"

    artefato_tamanho_maximo_bytes: int = 2 * 1024 * 1024
    """Teto por artefato quando STORAGE_BACKEND=postgres.

    O free tier do Neon dá 0,5 GB. Guardar artefato em `bytea` só é aceitável
    com um teto explícito, e com ele visível na interface, não escondido.
    """

    # ── Segurança ────────────────────────────────────────────────────────────
    jwt_secret: str = "desenvolvimento-apenas-troque-isto"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60
    internal_shared_secret: str = "desenvolvimento-apenas-troque-este-segredo-interno"

    # ── URLs dos serviços (modo distributed) ─────────────────────────────────
    identity_service_url: str = "http://localhost:8001"
    catalog_service_url: str = "http://localhost:8002"
    execution_service_url: str = "http://localhost:8003"
    runner_service_url: str = "http://localhost:8004"

    # ── Motor de execução ────────────────────────────────────────────────────
    fila_visibilidade_segundos: int = 300
    """Quanto tempo uma mensagem fica invisível depois de entregue.

    Se o worker morrer antes do `ack`, a mensagem reaparece após esse prazo. É
    o equivalente em PostgreSQL ao *redelivery* do RabbitMQ.
    """
    fila_max_tentativas: int = 3
    fila_intervalo_poll_segundos: float = 1.0
    execucao_timeout_padrao_segundos: int = 900

    # ── Demonstração ─────────────────────────────────────────────────────────
    demo_mode: bool = False
    """Habilita entrada sem senha por papel. Jamais com dado real."""

    log_json: bool = False
    log_nivel: str = "INFO"

    @field_validator("database_url", mode="before")
    @classmethod
    def _normaliza_url_do_banco(cls, valor: str) -> str:
        return normalizar_database_url(valor)

    @property
    def modo_monolito(self) -> bool:
        return self.hub_mode == "monolith"

    @property
    def url_banco_bruta(self) -> str:
        """A URL como veio do ambiente, ainda com os parâmetros de libpq."""
        import os

        return os.getenv("DATABASE_URL") or self.database_url


def carregar_dotenv_no_ambiente(caminho: str = ".env") -> int:
    """Copia o `.env` para `os.environ`, sem sobrescrever o que já existe.

    O `pydantic-settings` lê o `.env` por conta própria, mas guarda os valores
    **apenas** no objeto `Settings`. Isso não basta para duas partes do
    projeto que leem direto do ambiente:

    * o **portal Django**, cuja configuração usa `os.getenv` (e que não deveria
      importar `hub_core.settings` só para ler a própria `SECRET_KEY`);
    * o **runner**, que resolve os valores de segredo das automações de
      `os.environ` no momento de montar o container.

    Sem esta função, `make up` funciona (o compose injeta as variáveis no
    container) e rodar `uvicorn` à mão não, um ambiente de desenvolvimento
    que se comporta diferente do de produção pelo motivo mais bobo possível.

    Não sobrescreve: variável já definida no ambiente ganha do arquivo, que é
    a precedência que se espera e o que permite `LOG_NIVEL=DEBUG make mono`.

    Devolve quantas variáveis foram definidas.
    """
    import os
    from pathlib import Path

    arquivo = Path(caminho)
    if not arquivo.is_file():
        return 0

    definidas = 0
    for linha_bruta in arquivo.read_text(encoding="utf-8").splitlines():
        linha = linha_bruta.strip()
        if not linha or linha.startswith("#") or "=" not in linha:
            continue
        nome, _, valor = linha.partition("=")
        nome = nome.strip()
        valor = valor.strip()
        # Remove aspas envolventes, se houver. Sem isso, `X="a b"` chegaria ao
        # processo filho com as aspas dentro do valor.
        if len(valor) >= 2 and valor[0] == valor[-1] and valor[0] in "\"'":
            valor = valor[1:-1]
        if nome and nome not in os.environ:
            os.environ[nome] = valor
            definidas += 1
    return definidas


@lru_cache
def obter_settings() -> Settings:
    """Settings em cache de processo. Em teste, chame `obter_settings.cache_clear()`."""
    carregar_dotenv_no_ambiente()
    return Settings()
