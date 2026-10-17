# 0004 - Lock distribuído de um nó, sem Redlock

**Situação:** aceita · **Data:** 2026-10-17
**Implementa:** `hub_core/locks/`

## Contexto

Dois lugares precisam de exclusão mútua entre processos:

- o **agendador**, que roda em todas as réplicas do `execution-service`, sem
  lock, o agendamento das 6h dispara uma vez por réplica;
- o **controle de admissão**, onde contar execuções ativas e inserir a nova
  precisam ser atômicos entre processos. Com uma réplica, um `SELECT … FOR
  UPDATE` bastaria; com várias, não há linha comum para travar, porque a
  contagem é sobre linhas que ainda não existem.

## Decisão

Lock de **um nó**: advisory lock do PostgreSQL (padrão) ou uma chave no Redis.
Não implementamos Redlock, o algoritmo de quórum entre vários nós Redis.

A liberação no adaptador Redis usa script Lua para comparar-e-apagar
atomicamente. Um `DEL` simples apagaria o lock mesmo depois de ele ter
expirado e sido retomado por outro processo, o processo lento apagaria o lock
do rápido.

As duas semânticas **não são idênticas**, e isso está documentado em vez de
escondido:

| | Redis | PostgreSQL |
|---|---|---|
| Expiração | TTL na chave | vive com a conexão |
| Processo morre | espera o TTL | libera **na hora** |
| `ttl_segundos` | respeitado | ignorado, explicitamente |

Para o uso deste projeto, a liberação imediata do PostgreSQL é melhor: o
trabalho é retomado em segundos em vez de esperar um TTL vencer.

## Consequências

**A favor.** Implementação pequena e auditável. O adaptador PostgreSQL não
acrescenta dependência nenhuma.

**Contra.** Com um único nó Redis, um failover pode perder o lock e permitir
dois detentores. O pior caso é um agendamento disparado duas vezes.

Isso é aceito porque o motor já trata disparo repetido: o controle de admissão
recusa a segunda execução pelo limite de concorrência, e a mensagem duplicada
na fila encontra a execução já em andamento. A garantia do lock é uma
otimização, não a única barreira.

## Alternativas

- **Redlock.** Exige cinco nós Redis para valer a pena e tem fragilidades
  conhecidas em cenários de relógio e pausa de GC, desproporcional aqui.
- **Eleição de líder (etcd, ZooKeeper).** Correto e um componente de
  infraestrutura a mais, que não existe no free tier.
- **Nenhum lock, com idempotência em tudo.** Teria de ser o caminho se o
  disparo duplicado fosse inaceitável; hoje ele é apenas indesejável, e o lock
  resolve 99% dos casos por um custo muito menor.
