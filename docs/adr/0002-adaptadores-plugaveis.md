# 0002 - Toda dependência de infraestrutura é uma porta com dois adaptadores

**Situação:** aceita · **Data:** 2026-10-17
**Implementa:** `hub_core/broker/`, `executor/`, `storage/`, `locks/`

## Contexto

A plataforma precisa rodar em dois ambientes com restrições opostas:

| | Produção / desenvolvimento | Demonstração pública |
|---|---|---|
| Fila | RabbitMQ | nada além de PostgreSQL |
| Execução | containers Docker | sem acesso ao Docker Engine |
| Artefatos | SeaweedFS / S3 | sem object storage |
| Lock | Redis | sem Redis |
| Memória | o que for preciso | 512 MB |

Cortar funcionalidade na demonstração resolveria, e esvaziaria o projeto: o
que há de interessante para mostrar é justamente a fila com retry, o
isolamento da execução e o controle de admissão.

## Decisão

Cada uma das quatro dependências é uma **classe abstrata** (`Broker`,
`Executor`, `ArtefatoStore`, `Lock`) com ao menos duas implementações. A que
roda vem de uma variável de ambiente (`BROKER_BACKEND`, `EXECUTOR_BACKEND`,
`STORAGE_BACKEND`, `LOCK_BACKEND`).

Duas regras que fazem o desenho funcionar:

1. **A interface é desenhada a partir do que os dois adaptadores conseguem
   garantir**, não a partir do que o mais capaz oferece. Por isso o `Broker`
   não tem `exchange`, `routing key` nem consumo por *push*: são conceitos sem
   equivalente honesto em SQL, e o adaptador PostgreSQL teria de simulá-los
   mal.
2. **Onde a capacidade genuinamente não existe nos dois lados, a porta admite
   isso** em vez de fingir uniformidade. `ArtefatoStore.url_para_download`
   devolve `None` quando não há URL pré-assinada, e quem chama trata os dois
   casos. Uma abstração que escondesse a diferença faria o adaptador
   PostgreSQL servir URLs que não funcionam.

## Consequências

**A favor.** O mesmo código roda nos dois ambientes. Cada adaptador é testável
isoladamente. Trocar RabbitMQ por SQS é escrever uma classe.

**Contra.** Quatro abstrações a mais para quem lê o código pela primeira vez.
A interface é o menor denominador comum: quem tem RabbitMQ não acessa
*delayed exchange* nem *consumer groups* pela porta. E o adaptador menos usado
é o que menos recebe atenção, por isso os testes de integração rodam contra
o adaptador PostgreSQL, que é o que vai para produção na demonstração.

## Alternativas

- **Dois códigos, um por ambiente.** Divergem em um mês e passam a esconder
  bugs um do outro.
- **Só o stack completo, e demonstração em vídeo.** Um link que o visitante
  clica vale mais do que um vídeo que ele não assiste.
- **Só o stack mínimo (PostgreSQL para tudo).** Simplificaria, e o projeto
  deixaria de mostrar que se sabe operar RabbitMQ, Redis e Docker, que é
  parte do que ele existe para mostrar.
