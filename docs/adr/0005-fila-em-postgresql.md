# 0005 - Fila de mensagens sobre uma tabela PostgreSQL

**Situação:** aceita · **Data:** 2026-10-17
**Implementa:** `hub_core/broker/postgres.py`, `hub_core/db/plataforma.py`

## Contexto

A plataforma precisa de fila durável com retry, backoff, *visibility timeout*,
prioridade e fila de mensagens mortas. O ambiente de demonstração não tem
broker, e não vai ter, porque não existe RabbitMQ em free tier.

"Fila em banco de dados" tem má fama merecida: a implementação ingênua
(`SELECT … WHERE processado = false LIMIT 1` seguido de `UPDATE`) entrega a
mesma mensagem a dois consumidores.

## Decisão

Uma tabela, `plataforma.fila_mensagens`, consumida com **`SELECT … FOR UPDATE
SKIP LOCKED`** dentro de um CTE que alimenta o `UPDATE` de entrega, um
comando só, uma transação só, sem janela em que a mensagem esteja travada mas
ainda visível a outro worker.

`SKIP LOCKED` é o que torna isso viável. `FOR UPDATE` sozinho faz o segundo
consumidor **esperar** o primeiro, serializando os workers e matando o
paralelismo; `SKIP LOCKED` manda ignorar as linhas já travadas e seguir para
as próximas. Foi exatamente para isso que o PostgreSQL 9.5 o introduziu, e é o
padrão usado por `que` (Ruby), `graphile-worker` (Node) e `River` (Go).

O resto decorre:

| Garantia | Como |
|---|---|
| Visibility timeout | coluna `invisivel_ate` + recuperador periódico |
| Retry com backoff | `disponivel_em` no futuro |
| DLQ | estado `morta` |
| Prioridade | `ORDER BY prioridade DESC` com índice parcial |
| Atraso (agendamento) | `disponivel_em = now() + intervalo` |

Nada é apagado no caminho feliz: a mensagem muda de estado. Isso mantém o
histórico, permite medir tempo de espera e torna o retry auditável. A limpeza
é um job separado com retenção explícita, não um `DELETE` escondido no `ack`.

Duas escolhas de índice que importam: um índice **parcial** sobre as pendentes
(em uma fila com milhões de linhas concluídas, um índice sobre a tabela
inteira cresce sem limite) e outro parcial sobre as em processamento, para o
recuperador.

## Consequências

**A favor.** Zero dependência além do banco que já existe. A fila é
inspecionável com SQL, algo que o RabbitMQ não dá. O histórico fica.

**Contra.** O consumo é por **poll**, com latência de até
`FILA_INTERVALO_POLL_SEGUNDOS` (1 s por padrão), contra o *push* do RabbitMQ.
O teto de vazão fica na casa de milhares de mensagens por minuto, ordens de
magnitude abaixo do broker, e ordens de magnitude acima do que este projeto
precisa. E a fila disputa conexões e I/O com o banco de domínio, então um pico
de fila pode degradar consulta de tela.

**Uma sutileza que custa um teste para descobrir:** com `SKIP LOCKED`,
`LIMIT n` pode devolver **menos** que `n`, porque o limite é aplicado à
varredura e não às linhas efetivamente travadas. Pedir 10 sob contenção pode
trazer 3. `consumir` promete "até `limite`", e o worker usa `limite=1` com um
laço justamente para não depender disso. Registrado em
`tests/test_integracao.py::test_lote_parcial_sob_concorrencia`.

## Alternativas

- **`LISTEN`/`NOTIFY`.** Elimina o poll e não é durável: notificação enviada
  enquanto ninguém escuta se perde. Serviria como complemento para reduzir
  latência, com o poll como rede de segurança, não foi implementado.
- **Celery com broker em banco.** Traz um modelo de execução inteiro para
  usar a fila, e o resultado seria mais acoplado, não menos.
- **SQS, Cloud Tasks.** Resolveriam e trariam dependência de nuvem específica,
  justamente o que a porta plugável existe para evitar.
