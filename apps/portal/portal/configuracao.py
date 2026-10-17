"""Configuração do portal Django.

## O portal não tem banco de dados

`DATABASES = {}`. Não é economia: é a consequência de o portal ser um **BFF**
(Backend For Frontend) e nada mais. Ele não tem modelo, não tem migração e não
tem tabela. Tudo que mostra vem dos quatro serviços por HTTP; a sessão vive em
um cookie assinado.

Três coisas caem fora por causa disso, e todas são deliberadas:

* **`django.contrib.auth`** — a identidade é do `identity-service`, que emite
  JWT. Usar o `auth` do Django significaria duas bases de usuário para manter
  em sincronia, que é o problema que ter um serviço de identidade resolve;
* **`django.contrib.admin`** — depende do `auth` e de banco. A administração
  da plataforma é feita pelas telas próprias, que respeitam o mesmo RBAC do
  resto;
* **sessão em banco** — `signed_cookies` guarda a sessão no próprio cookie,
  assinada com a `SECRET_KEY`. O limite é 4 KB; o JWT ocupa cerca de 700
  bytes, com folga confortável.

O resultado é um processo sem estado, que escala horizontalmente sem nada
compartilhado e sobe em segundos — o que importa em um free tier que hiberna.

## Por que Django, e não FastAPI com Jinja

O portal poderia ser mais um `FastAPI`, e aí seria um framework só no projeto.
Django entra pelo que traz pronto e que teria de ser escrito à mão: sistema de
templates com herança, `messages` framework, proteção CSRF, `staticfiles` com
hashing para cache, internacionalização e um roteamento por app que mantém
telas de domínios diferentes separadas.

A contrapartida é carregar um framework grande para usar uma fração dele. Em
troca, `apps/portal` tem umas 1 500 linhas em vez de 4 000. Ver docs/adr/0012.
"""

from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# ── Segurança ────────────────────────────────────────────────────────────────

SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "desenvolvimento-apenas-troque-esta-chave-em-producao")

DEBUG = os.getenv("DJANGO_DEBUG", "false").lower() in {"true", "1", "yes"}

ALLOWED_HOSTS = [
    h.strip()
    for h in os.getenv("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
    if h.strip()
]

# O Render serve a aplicação atrás de um proxy que termina o TLS. Sem estas
# duas linhas, o Django acha que a requisição é HTTP e monta URL absoluta com
# `http://`, o que o navegador bloqueia como conteúdo misto — e a proteção
# CSRF rejeita o POST por origem divergente.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = True

CSRF_TRUSTED_ORIGINS = [
    o.strip() for o in os.getenv("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if o.strip()
]
# O Render expõe o host externo nesta variável; aproveitá-la evita que cada
# deploy precise de configuração manual de origem confiável.
if host_render := os.getenv("RENDER_EXTERNAL_HOSTNAME"):
    ALLOWED_HOSTS.append(host_render)
    CSRF_TRUSTED_ORIGINS.append(f"https://{host_render}")

if not DEBUG:
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_REFERRER_POLICY = "same-origin"
    X_FRAME_OPTIONS = "DENY"

# `Lax` e não `Strict`: com `Strict`, o cookie não é enviado quando alguém
# chega ao portal por um link externo — e a pessoa cai na tela de login mesmo
# com sessão válida. `Lax` protege contra CSRF em requisição que altera estado
# (que é POST) e mantém a navegação por link funcionando.
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"

# ── Aplicações ───────────────────────────────────────────────────────────────

INSTALLED_APPS = [
    "django.contrib.staticfiles",
    "django.contrib.messages",
    # Sem `auth`, sem `admin`, sem `contenttypes`: ver docstring do módulo.
    "nucleo",
    "contas",
    "marketplace",
    "execucoes",
    "painel",
    "administracao",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # Carrega a identidade do token em `request.quem`, e renova o que precisa
    # ser renovado. Depois de `session`, porque lê a sessão.
    "nucleo.middleware.IdentidadeMiddleware",
]

ROOT_URLCONF = "portal.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.messages.context_processors.messages",
                # Sem `auth.context_processors.auth`: não há `django.contrib.auth`.
                # `nucleo.contexto.identidade` põe `quem` e `permissoes` em todo
                # template, que é o que o `auth` faria com `user`.
                "nucleo.contexto.identidade",
                "nucleo.contexto.plataforma",
            ],
            # Em produção, o loader em cache compila cada template uma vez. Sem
            # ele, cada requisição relê o arquivo do disco — o que em um
            # container com disco de rede é a maior parte do tempo de resposta.
            "loaders": (
                [
                    (
                        "django.template.loaders.cached.Loader",
                        [
                            "django.template.loaders.filesystem.Loader",
                            "django.template.loaders.app_directories.Loader",
                        ],
                    )
                ]
                if not DEBUG
                else None
            ),
        },
    }
]

# `loaders` e `APP_DIRS` são mutuamente exclusivos; o Django recusa os dois
# juntos. Em DEBUG queremos `APP_DIRS` (recarrega o template ao salvar), em
# produção queremos o loader em cache.
if not DEBUG:
    TEMPLATES[0]["APP_DIRS"] = False
else:
    TEMPLATES[0]["OPTIONS"].pop("loaders", None)

ASGI_APPLICATION = "portal.asgi.application"
WSGI_APPLICATION = "portal.wsgi.application"

# ── Banco: nenhum ────────────────────────────────────────────────────────────

DATABASES: dict[str, dict[str, object]] = {}

SESSION_ENGINE = "django.contrib.sessions.backends.signed_cookies"
SESSION_COOKIE_NAME = "orbita_sessao"
SESSION_COOKIE_HTTPONLY = True
# Igual à validade do JWT: deixar a sessão viver mais do que o token só produz
# uma sessão "válida" cujo token o serviço recusa.
SESSION_COOKIE_AGE = int(os.getenv("JWT_EXPIRE_MINUTES", "60")) * 60
SESSION_SAVE_EVERY_REQUEST = True

MESSAGE_STORAGE = "django.contrib.messages.storage.cookie.CookieStorage"

# ── Internacionalização ──────────────────────────────────────────────────────

LANGUAGE_CODE = "pt-br"
TIME_ZONE = "America/Sao_Paulo"
USE_I18N = True
USE_TZ = True

# ── Arquivos estáticos ───────────────────────────────────────────────────────

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        # `Manifest...` põe o hash do conteúdo no nome do arquivo
        # (`orbita.4f2a1b.css`), o que permite servir com cache longo e
        # invalidar na publicação. Exige `collectstatic`, rodado no Dockerfile.
        #
        # Não é o WhiteNoise. A middleware dele é **somente síncrona** — não
        # declara `async_capable` — e colocá-la na cadeia faz o Django adaptar
        # todo o resto para modo síncrono, quebrando as views assíncronas de
        # que o modo monolito depende. Os arquivos estáticos são servidos pela
        # camada ASGI em `portal.asgi`, que é nativa assíncrona.
        "BACKEND": "django.contrib.staticfiles.storage.ManifestStaticFilesStorage"
        if not DEBUG
        else "django.contrib.staticfiles.storage.StaticFilesStorage"
    },
}

# ── Integração com os serviços ───────────────────────────────────────────────

SERVICOS = {
    "identity": os.getenv("IDENTITY_SERVICE_URL", "http://localhost:8001"),
    "catalog": os.getenv("CATALOG_SERVICE_URL", "http://localhost:8002"),
    "execution": os.getenv("EXECUTION_SERVICE_URL", "http://localhost:8003"),
    "runner": os.getenv("RUNNER_SERVICE_URL", "http://localhost:8004"),
}

# Em modo monolito, o portal fala com os serviços pelo transporte in-process do
# `hub_core.clients`, sem rede. Em distribuído, por HTTP. O código das views é
# o mesmo — ver `nucleo.bff`.
HUB_MODE = os.getenv("HUB_MODE", "monolith")

DEMO_MODE = os.getenv("DEMO_MODE", "false").lower() in {"true", "1", "yes"}

AMBIENTE = os.getenv("AMBIENTE", "local")

# Intervalo de atualização do acompanhamento ao vivo, em milissegundos. Dois
# segundos é o equilíbrio encontrado: menos que isso gera requisição a cada
# piscada de olho sem a pessoa perceber diferença; mais que isso faz o log
# parecer travado.
INTERVALO_POLL_MS = int(os.getenv("PORTAL_INTERVALO_POLL_MS", "2000"))

LOGIN_URL = "/entrar"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"simples": {"format": "%(levelname)s %(name)s %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "simples"}},
    "root": {"handlers": ["console"], "level": os.getenv("LOG_NIVEL", "INFO")},
    "loggers": {
        # O Django registra cada 4xx como WARNING com o caminho completo. Em um
        # portal com polling HTMX, um 404 esperado polui o log a cada 2 s.
        "django.request": {"level": "ERROR", "handlers": ["console"], "propagate": False},
    },
}
