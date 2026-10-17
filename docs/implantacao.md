# Implantação - Neon + Render

Guia para pôr o Órbita no ar de graça, sem cartão de crédito. O resultado é um
endereço público rodando em modo monolito: um processo, PostgreSQL para tudo,
executor simulado.

Tempo estimado: 15 minutos.

---

## Por que esses dois

**Neon** para o banco. PostgreSQL 16 de verdade, não um subconjunto -, free
tier permanente com 0,5 GB, e *scale-to-zero* que religa em cerca de um
segundo. O concorrente óbvio, Supabase, tem um problema que inviabiliza um
link de portfólio: projeto no free tier **é pausado após cerca de uma semana
sem acesso** e precisa ser religado à mão. O recrutador que abre o link em um
mês encontra um erro.

**Render** para a aplicação. Serviço web Docker gratuito, sem cartão. Hiberna
após 15 minutos sem acesso e acorda em cerca de 50 segundos, é o custo real
do plano, e está declarado na própria tela inicial da demonstração.

O código não depende de nenhum dos dois. `DATABASE_URL` e um runtime que rode
contêiner é tudo que ele pede.

---

## 1. Banco no Neon

1. Crie uma conta em [neon.tech](https://neon.tech) (entra com GitHub).
2. **Create project**, região `AWS South America (São Paulo)` se o público
   for brasileiro; a latência até o Render em Oregon some perto do tempo de
   acordar do container.
3. Em **Connection string**, copie a URL e **marque "Pooled connection"**.

Ela vem assim:

```
postgresql://usuario:senha@ep-algo-a1b2c3-pooler.sa-east-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require
```

### Por que o endpoint *pooled*

O free tier tem poucas conexões diretas. O endpoint `-pooler` é um PgBouncer
em modo transação, que multiplexa.

E aí mora a armadilha que custa uma tarde: **o PgBouncer em modo transação não
suporta prepared statements nomeados**, que o asyncpg usa por padrão. A
segunda query de cada conexão falha com

```
prepared statement "__asyncpg_stmt_1__" does not exist
```

O código detecta o pooler pelo `-pooler.` no host e desliga o cache de
statements sozinho (`hub_core/db/engine.py`). Também usa `NullPool` nesse
caso: manter um pool próprio atrás de um pooler externo são dois níveis
disputando o mesmo limite.

Os parâmetros `sslmode` e `channel_binding` na URL são sintaxe de libpq e o
asyncpg não os conhece, `hub_core/settings.py` os remove e reconstrói a
intenção de TLS em `connect_args`. **Cole a URL como veio**, sem editar.

---

## 2. Aplicação no Render

1. Suba o repositório para o GitHub.
2. Em [render.com](https://render.com): **New → Blueprint**.
3. Aponte para o repositório. O Render lê `infra/render.yaml`.
4. Ele vai pedir **uma** variável: `DATABASE_URL`. Cole a URL do Neon.
5. **Apply**.

`JWT_SECRET`, `INTERNAL_SHARED_SECRET` e `DJANGO_SECRET_KEY` são gerados
automaticamente (`generateValue: true`) e mantidos entre deploys, melhor do
que um valor no arquivo, e melhor do que pedir ao operador que invente um.

As migrações rodam no `preDeployCommand`, antes do processo subir. Em um
serviço só isso é seguro: não há corrida entre réplicas, porque o free tier
tem uma. Com mais de uma instância, viraria um job separado.

---

## 3. Carregar o cenário fictício

O deploy sobe a aplicação com o banco vazio. Para popular, abra o **Shell** do
serviço no painel do Render:

```bash
python -m scripts.seed --historico
```

Saem 10 unidades, 3 grupos, 10 pessoas, as 3 automações e 60 dias de execuções
passadas, para o painel ter série temporal, taxa de sucesso e ranking.

O comando imprime os CPFs de entrada. É idempotente: rodar de novo não
duplica nada.

---

## 4. Conferir

| | |
|---|---|
| `https://<seu-app>.onrender.com/saude` | `{"situacao":"ok","componente":"portal",…}` |
| `/_plataforma/saude` | adaptadores em uso |
| `/_plataforma/docs` | índice dos `/docs` dos quatro serviços |
| `/` | catálogo (redireciona para a entrada) |

Na tela de entrada, **ver a demonstração** entra sem senha assumindo um papel.

---

## O que muda em relação ao local

| | Local (`make up`) | Render |
|---|---|---|
| `HUB_MODE` | `distributed` | `monolith` |
| Processos | 5 | 1 |
| Fila | RabbitMQ | tabela PostgreSQL |
| Executor | Docker | simulado |
| Artefatos | MinIO | `bytea`, teto de 1 MB |
| Lock | Redis | advisory lock |
| Concorrência do runner | 2 | 1 |

**É o mesmo código.** As diferenças são variáveis de ambiente, ver
[ADR 0002](adr/0002-adaptadores-plugaveis.md).

---

## Operação

### O primeiro acesso demora

Container hibernado: cerca de 50 segundos. O banco acorda junto, em cerca de
um segundo. Os acessos seguintes são imediatos. A tela de entrada avisa.

Manter o serviço acordado com um pinger externo é possível e consome a cota de
750 horas/mês do plano, o que faria o serviço parar antes do fim do mês. Não
vale.

### Teto de armazenamento

`ARTEFATO_TAMANHO_MAXIMO_BYTES` é 1 MB no Render. Os 0,5 GB do Neon são
compartilhados entre dado de domínio e artefato. Para arquivos maiores,
`STORAGE_BACKEND=s3` com Cloudflare R2 (10 GB grátis) resolve, e não exige
nenhuma mudança de código, só três variáveis.

### Limpeza

Os artefatos e as mensagens de fila concluídas acumulam. Há rotina pronta:

```python
await broker.limpar_concluidas(dias=30)
```

Não está agendada por padrão. Em um ambiente de longa duração, vale um gatilho
cron apontando para ela.

### Logs

`LOG_JSON=true` no Render: uma linha JSON por evento, com `trace_id`
propagado. Para seguir uma requisição que passou por três serviços, filtre
pelo `trace_id`, ele aparece no cabeçalho `x-trace-id` da resposta e na tela
de cada execução.

---

## Alternativas de hospedagem

Nenhuma delas exige mudança de código.

| Onde | Observação |
|---|---|
| **Google Cloud Run** | Free tier generoso, cold start de ~2 s em vez de 50. Exige cartão cadastrado (sem cobrança dentro do free tier). É o que eu usaria se o cartão não fosse impedimento |
| **Fly.io** | Boa latência no Brasil (GRU). O plano gratuito foi descontinuado |
| **Railway** | Simples, com crédito de teste e sem camada gratuita permanente |
| **Kubernetes** | O alvo da arquitetura. `HUB_MODE=distributed`, cinco Deployments, a mesma imagem com comandos diferentes. Só o runner precisa de ServiceAccount com permissão de criar Jobs |
