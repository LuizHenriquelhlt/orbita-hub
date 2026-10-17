# Decisões arquiteturais

Cada decisão que alguém poderia razoavelmente ter tomado de outro jeito está
registrada aqui e **referenciada por comentário no ponto do código que a
implementa**. A referência cruzada é o que mantém as duas pontas vivas.

Toda ADR declara o que se **perdeu** ao escolher. Decisão sem custo declarado
é decisão que ninguém examinou de verdade.

| | Decisão | O que se perdeu |
|---|---|---|
| [0001](0001-registro-de-decisoes.md) | Registrar decisões, e registrá-las curtas | ADR desatualizada é pior que ADR nenhuma |
| [0002](0002-adaptadores-plugaveis.md) | Toda dependência de infraestrutura é uma porta com dois adaptadores | Interface limitada ao menor denominador comum |
| [0003](0003-schema-por-servico.md) | Um schema por serviço, sem FK entre schemas | Consulta entre domínios custa chamada HTTP |
| [0004](0004-lock-sem-redlock.md) | Lock distribuído de um nó, sem Redlock | Failover do Redis pode permitir dois detentores |
| [0005](0005-fila-em-postgresql.md) | Fila de mensagens sobre uma tabela PostgreSQL | Consumo por poll; teto de vazão bem menor |
| [0006](0006-protocolo-por-stdout.md) | A automação fala pelo stdout, não por HTTP | Canal unidirecional |
| [0007](0007-jwt-sem-revogacao.md) | JWT validado localmente | Revogação só vale no próximo token |
| [0008](0008-requisitos-de-publicacao.md) | Requisitos de publicação verificáveis | Sem scan de vulnerabilidade da imagem |
| [0009](0009-parser-de-cron-proprio.md) | Parser de cron próprio, subconjunto POSIX | Código a manter, com casos de borda |
| [0010](0010-publicacao-apos-commit.md) | Publicar na fila depois do commit | Janela em que a execução existe sem mensagem |
| [0011](0011-imagem-unica.md) | Uma imagem Docker para os cinco artefatos | Cada container carrega dependência que não usa |
| [0012](0012-portal-em-django.md) | Portal em Django, sem banco próprio | Dois frameworks web no projeto |
| [0013](0013-polling-em-vez-de-websocket.md) | Polling com ponteiro de sequência | Latência de até 2 s |
| [0014](0014-sistema-de-arquivos-do-container.md) | Volumes, e não tmpfs, para falar com o container | Acoplamento a detalhe da API do Docker |

## As três que mais mudam a forma do sistema

Se for ler só três: [0002](0002-adaptadores-plugaveis.md) explica por que o
mesmo código roda em dois ambientes opostos; [0003](0003-schema-por-servico.md)
explica por que a fronteira entre serviços é verificável e não apenas
documentada; [0005](0005-fila-em-postgresql.md) explica como uma tabela
substitui um broker sem virar gambiarra.
