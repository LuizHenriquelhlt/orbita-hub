# Imagem única para os cinco artefatos (quatro serviços + gateway/portal).
#
# Uma imagem só, e não cinco, por um motivo concreito: os quatro serviços
# compartilham `hub_core` e quase todas as dependências. Cinco imagens
# significariam cinco builds que baixam o mesmo `sqlalchemy`, cinco caches a
# invalidar e cinco tags a manter em sincronia — para economizar poucos
# megabytes de código Python. O que diferencia um container do outro é o
# comando, não o conteúdo.
#
# Em uma instalação grande, com times donos de serviços diferentes, a conta
# inverteria e valeria separar. Ver docs/adr/0011.

# ── Estágio de build ─────────────────────────────────────────────────────────
FROM python:3.12-slim AS build

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Dependências de compilação. Ficam só neste estágio: a imagem final não
# precisa de gcc, e carregá-lo aumenta a superfície de ataque sem ganho.
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

# Copiar só os arquivos de metadados antes do código é o que faz o cache de
# camada funcionar: mudar uma linha de Python não reinstala as dependências.
COPY pyproject.toml README.md ./
COPY packages/hub-core/hub_core/__init__.py packages/hub-core/hub_core/__init__.py
COPY services/identity/hub_identity/__init__.py services/identity/hub_identity/__init__.py
COPY services/catalog/hub_catalog/__init__.py services/catalog/hub_catalog/__init__.py
COPY services/execution/hub_execution/__init__.py services/execution/hub_execution/__init__.py
COPY services/runner/hub_runner/__init__.py services/runner/hub_runner/__init__.py
COPY gateway/hub_gateway/__init__.py gateway/hub_gateway/__init__.py
COPY sdk/hub_sdk/__init__.py sdk/hub_sdk/__init__.py

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

ARG EXTRAS="portal,rabbitmq,redis,s3,docker"
RUN pip install --upgrade pip setuptools wheel \
 && pip install ".[${EXTRAS}]"

# ── Estágio final ────────────────────────────────────────────────────────────
FROM python:3.12-slim AS final

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    TZ=America/Sao_Paulo

# `curl` para o healthcheck do compose; `tzdata` porque o agendador interpreta
# cron no fuso de São Paulo e sem o banco de fusos o `zoneinfo` falha.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl tzdata \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 1000 orbita

COPY --from=build /opt/venv /opt/venv

WORKDIR /app
COPY --chown=orbita:orbita . .

# Reinstala em modo editável para que o código copiado (e não a cópia feita no
# estágio de build, que só tinha os `__init__.py`) seja o que roda.
RUN pip install --no-deps -e .

# Coleta os estáticos com o hash do conteúdo no nome, permitindo cache longo.
# Roda no build, não na inicialização: o container do free tier hiberna e
# acorda a cada acesso, e fazer isso a cada despertar somaria segundos ao
# tempo de resposta a frio.
#
# `DJANGO_SECRET_KEY` aqui é descartável: o `collectstatic` exige que a
# configuração carregue, mas não assina nada. A chave real vem do ambiente em
# tempo de execução.
ENV DJANGO_SETTINGS_MODULE=portal.configuracao \
    PYTHONPATH=/app/apps/portal

RUN DJANGO_SECRET_KEY=apenas-para-coletar-estaticos \
    DJANGO_DEBUG=false \
    python apps/portal/manage.py collectstatic --noinput --clear \
 && chown -R orbita:orbita /app

USER orbita

EXPOSE 8000 8001 8002 8003 8004

HEALTHCHECK --interval=20s --timeout=5s --start-period=25s --retries=3 \
  CMD curl -fsS "http://localhost:${PORTA:-8000}/saude" || exit 1

# O comando é dado pelo compose ou pelo render.yaml: é o que diferencia um
# container do outro.
CMD ["uvicorn", "hub_gateway.main:app", "--host", "0.0.0.0", "--port", "8000"]
