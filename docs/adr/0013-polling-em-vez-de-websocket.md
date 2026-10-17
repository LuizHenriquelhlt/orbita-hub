# 0013 - Acompanhamento ao vivo por polling com ponteiro de sequência

**Situação:** aceita · **Data:** 2026-10-17
**Implementa:** `apps/portal/execucoes/`, `hub_execution/motor.py`

## Contexto

A tela de uma execução mostra log, status, métricas e artefatos **enquanto ela
roda**. O desenho natural é WebSocket, e é o que a arquitetura-alvo previa.

## Decisão

Polling com HTMX e um **ponteiro de sequência**:

1. a página carrega com o log existente e guarda a última `sequencia`;
2. a cada 2 s, pede `/execucoes/<id>/log?desde=<sequencia>`;
3. o serviço devolve **só** as linhas novas, e o HTMX as acrescenta ao fim
   (`hx-swap="beforeend"`);
4. quando a execução termina, o fragmento devolvido **não contém** o atributo
   de polling, e o HTMX para de pedir.

O passo 3 é o que torna isso viável: sem o ponteiro, cada atualização
rebaixaria o log inteiro. Em uma execução com 2 000 linhas, seriam alguns
megabytes a cada 2 s.

O passo 4 evita o bug clássico de polling: um laço que continua rodando para
sempre depois de a execução acabar. Quem decide parar é o **servidor**, que é
quem conhece o estado, não um contador no navegador.

Há uma simetria importante: o fragmento de **log** é acrescentado (só cresce)
e o de **estado** é substituído. Juntá-los retransmitiria todo o log a cada 2
s só para atualizar o rótulo de status. E os dois fragmentos trazem o próprio
invólucro com os atributos de gatilho, justamente para que a ausência deles
na resposta final interrompa o ciclo.

A sequência existe porque `ocorrido_em` não basta para ordenar: uma automação
que emite dezenas de linhas no mesmo milissegundo produz timestamps empatados,
e `ORDER BY ocorrido_em` devolve ordem arbitrária entre eles, embaralhando o
log justamente no trecho mais movimentado.

## Consequências

**A favor.** Nenhum processo com estado, nenhuma *sticky session*, nada a
reconfigurar quando há mais de uma réplica. Funciona atrás de qualquer proxy.
Reconexão é de graça: o navegador que ficou offline por 30 s manda o mesmo
ponteiro e recebe tudo que perdeu, um WebSocket precisaria implementar
reenvio a partir de um cursor, que é exatamente este ponteiro, só que mais
difícil.

**Contra.** Latência de até 2 s. E uma requisição a cada 2 s por aba aberta,
que com cem pessoas acompanhando execuções seriam 50 requisições por segundo
só de polling, a essa escala o WebSocket passa a valer. Com a carga deste
projeto, não vale.

## Alternativas

- **WebSocket.** Mais eficiente. Exige processo com estado (e o free tier
  hiberna), sessão presa à instância quando há réplicas, e reconexão com
  reenvio do que foi perdido.
- **Server-Sent Events.** Meio-termo real: unidirecional, sobre HTTP comum, e
  o protocolo tem `Last-Event-ID`, que é o mesmo ponteiro de sequência. Ainda
  exige conexão aberta por aba, o que no free tier compete com as poucas
  conexões disponíveis. É a evolução natural se a latência de 2 s incomodar.
- **Polling sem ponteiro.** Simples e insustentável: o log inteiro a cada
  ciclo.
