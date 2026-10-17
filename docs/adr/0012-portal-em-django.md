# 0012 — Portal em Django, sem banco próprio

**Situação:** aceita · **Data:** 2026-10-17
**Implementa:** `apps/portal/`

## Contexto

O portal é um **BFF**: o navegador conversa só com ele, e ele chama os quatro
serviços do lado servidor com o JWT guardado na sessão. Isso é o que mantém o
token fora do alcance do JavaScript — não há JWT em `localStorage`, que é o
alvo preferido de XSS em aplicação com API.

O resto do projeto é FastAPI. Usar FastAPI aqui também daria um framework só.

## Decisão

Django 5, e **sem banco de dados**: `DATABASES = {}`.

O Django entra pelo que traz pronto e que teria de ser escrito à mão: sistema
de templates com herança, framework de mensagens, proteção CSRF,
`staticfiles` com hashing para cache, internacionalização e roteamento por app
que mantém telas de domínios diferentes separadas. O resultado é
`apps/portal` com cerca de 1 500 linhas em vez de 4 000.

Sem banco é consequência de o portal ser BFF e nada mais. Caem fora:

- **`django.contrib.auth`** — a identidade é do `identity-service`; usar o
  `auth` significaria duas bases de usuário para manter em sincronia, que é o
  problema que ter um serviço de identidade resolve;
- **`django.contrib.admin`** — depende de `auth` e de banco. A administração é
  feita por telas próprias, que respeitam o mesmo RBAC do resto;
- **sessão em banco** — `signed_cookies` guarda a sessão no próprio cookie,
  assinada. O limite é 4 KB; o JWT ocupa cerca de 700 bytes.

As views são `async def`, exigência do transporte in-process do modo monolito.
Isso trouxe dois problemas que valem registro:

- **o WhiteNoise não declara `async_capable`**, e uma middleware síncrona na
  cadeia faz o Django adaptar todo o resto para modo síncrono. Os estáticos
  saíram da cadeia e são servidos por um `Mount` do Starlette em
  `portal/asgi.py`;
- **middleware assíncrono escrito como classe precisa de
  `markcoroutinefunction(self)`** no `__init__`. Sem isso, o Django o trata
  como síncrono e o erro aparece em `django/middleware/clickjacking.py`, um
  arquivo sem relação com o problema.

## Consequências

**A favor.** Processo sem estado, que escala horizontalmente sem nada
compartilhado e sobe em segundos — o que importa em um free tier que hiberna.
Nenhuma migração a rodar no portal.

**Contra.** Carrega um framework grande para usar uma fração dele. Dois
frameworks web no projeto, e quem for contribuir precisa conhecer os dois.
E `DATABASES = {}` é um caminho pouco trilhado do Django: metade dos tutoriais
e dos pacotes de terceiro assume banco, e qualquer app que se queira instalar
precisa ser verificado antes.

## Alternativas

- **FastAPI + Jinja2.** Um framework só. Exigiria reescrever à mão
  mensagens, CSRF, `staticfiles` com hashing e organização por app.
- **SPA em React/Next.js.** O desenho da arquitetura-alvo original. Traz um
  build de JavaScript, e o token teria de ir para o navegador — justamente o
  que o BFF evita.
- **Django com banco e `contrib.auth`.** Simplificaria a sessão e criaria a
  segunda base de usuários.
