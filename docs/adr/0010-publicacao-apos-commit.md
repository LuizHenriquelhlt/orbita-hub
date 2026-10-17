# 0010 — Publicar na fila depois do commit, com recuperação de órfãs

**Situação:** aceita · **Data:** 2026-10-17
**Implementa:** `hub_execution/motor.py`

## Contexto

Disparar uma execução são dois efeitos que precisariam ser atômicos:

1. gravar a linha de `execucao` no PostgreSQL;
2. publicar a mensagem na fila.

Não há transação que abranja os dois quando a fila é o RabbitMQ. Mesmo com a
fila em PostgreSQL, publicar dentro da transação do motor funcionaria — mas
tornaria o comportamento diferente entre os dois adaptadores, que é
justamente o que a porta plugável existe para evitar.

Há, portanto, duas ordens possíveis, cada uma com a sua falha:

| Ordem | Falha possível | Consequência |
|---|---|---|
| Publicar **antes** do commit | mensagem existe, execução não | o runner consome, procura a execução no banco, não encontra |
| Publicar **depois** do commit | execução existe, mensagem não | a execução fica em "na fila" para sempre |

## Decisão

Publicar **depois** do commit, e tratar a órfã.

O raciocínio é sobre qual falha é mais fácil de diagnosticar e corrigir. A
primeira produz um erro no runner apontando para um id que não existe — e
pode ser *intermitente*, porque depende de o runner consumir antes de o commit
terminar, o que varia com a carga. A segunda produz uma execução visível, em
um estado coerente, que um recuperador identifica com uma consulta trivial:
status `enfileirada`, `mensagem_id` nulo, enfileirada há mais de um minuto.

`motor.recuperar_orfas` roda no laço do agendador e republica essas
mensagens. Um minuto de folga é deliberado: tempo mais que suficiente para a
publicação normal concluir, evitando que a rotina corra atrás de execuções no
caminho feliz.

Se a publicação falhar de forma explícita (broker indisponível), a execução é
marcada como falha na hora, com a mensagem de erro — sem esperar o
recuperador.

## Consequências

**A favor.** Nunca existe mensagem apontando para execução inexistente. A
falha possível é visível, coerente e recuperável automaticamente.

**Contra.** Existe uma janela — da ordem de milissegundos — em que a execução
está gravada e não enfileirada. Se o processo morrer exatamente ali, a
execução espera até um minuto para ser recuperada. Para um disparo manual, é
um minuto em que a tela mostra "na fila" sem nada acontecendo.

Isto é um **remendo**, não a solução. A solução é *outbox* transacional.

## Alternativas

- **Outbox transacional.** A mensagem é gravada na mesma transação da
  execução, em uma tabela de saída, e um processo separado a publica. Elimina
  a janela por completo. Não foi implementado porque exige um publicador
  dedicado e, com a fila já em PostgreSQL no modo monolito, o ganho sobre o
  recuperador é pequeno. É o próximo passo natural se a plataforma crescer.
- **Publicar antes do commit, com retry no runner.** O runner reconsumiria a
  mensagem até a execução aparecer. Transforma uma condição de corrida em
  latência, e polui o log com erros que não são erros.
- **Fila em PostgreSQL sempre, publicando na mesma transação.** Resolveria de
  verdade — ao custo de tornar o adaptador RabbitMQ impossível, que é abrir
  mão da ADR 0002.
