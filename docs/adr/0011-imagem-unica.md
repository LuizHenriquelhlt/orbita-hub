# 0011 — Uma imagem Docker para os cinco artefatos

**Situação:** aceita · **Data:** 2026-10-17
**Implementa:** `infra/Dockerfile`

## Contexto

O projeto produz cinco artefatos executáveis: quatro serviços e o
gateway/portal. A prática usual em microsserviços é uma imagem por serviço.

## Decisão

Uma imagem só. O que diferencia um container do outro é o **comando**, não o
conteúdo.

Os quatro serviços compartilham `hub_core` e praticamente todas as
dependências. Cinco imagens significariam cinco builds baixando o mesmo
`sqlalchemy`, cinco caches a invalidar e cinco tags a manter em sincronia —
para economizar poucos megabytes de código Python.

Há um ganho adicional que importa no free tier: no modo monolito, o container
que roda é o **mesmo** que rodaria distribuído. Não existe "imagem de
demonstração" divergindo da de produção.

O Dockerfile é multi-estágio: o estágio de build carrega `build-essential`,
que não vai para a imagem final. E copia primeiro os arquivos de metadados e
só depois o código, para que mudar uma linha de Python não reinstale as
dependências.

## Consequências

**A favor.** Um build, um cache, uma tag. O modo monolito usa a mesma imagem.
A superfície de ataque da imagem final é menor, porque o compilador fica para
trás.

**Contra.** Cada container carrega as dependências dos outros: o
`identity-service` tem o SDK do Docker instalado e nunca o usa. São dezenas de
megabytes de pacote inútil por container, e uma superfície de ataque maior do
que a mínima para cada um.

A conta se inverte quando times diferentes passam a ser donos de serviços
diferentes: aí cada time quer o seu ciclo de release, e uma imagem comum vira
acoplamento de cronograma. Com um time, é o contrário.

## Alternativas

- **Uma imagem por serviço, com uma base comum.** O desenho correto em escala.
  Exige publicar e versionar a imagem base, e o cache de camada só ajuda se
  todos partirem da mesma versão dela.
- **Imagem distroless.** Reduz bastante a superfície de ataque. Complica o
  diagnóstico (sem shell no container) e exige mais do build — desproporcional
  neste estágio.
