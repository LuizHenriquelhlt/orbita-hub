# 0014 - Volumes, e não tmpfs, para falar com o container da automação

**Situação:** aceita · **Data:** 2026-10-17
**Implementa:** `hub_core/executor/docker_exec.py`

## Contexto

A plataforma precisa de duas coisas do container de uma automação: **entregar**
os parâmetros antes de ele iniciar e **colher** os artefatos que ele produz. As
duas coisas são feitas pela API de archive do daemon, `put_archive` e
`get_archive`, porque a alternativa, montar um diretório do host, deixaria o
host ao alcance de código de terceiro.

O desenho original era o mais direto possível: raiz somente leitura, um `tmpfs`
com teto em `/trabalho` para a área de trabalho, parâmetros gravados na camada
do container antes do start.

Nenhuma das duas coisas funcionou, e as duas falharam calado.

## Decisão

Dois volumes por execução, criados e removidos pelo executor:

| Caminho | Volume | Backing | Papel |
|---|---|---|---|
| `/orbita` | `orbita-ctl-<exec>` | disco | parâmetros, 0444 root:root |
| `/trabalho` | `orbita-wk-<exec>` | tmpfs, com teto | artefatos |

A raiz continua somente leitura. `/tmp` continua `tmpfs` do HostConfig, porque
nada precisa ler de volta o que a automação escreve ali.

### Por que não a camada do container

`read_only=True` marca o rootfs na **criação**, não no start. `put_archive` em
qualquer caminho da camada é recusado com `400 container rootfs is marked
read-only`, com o container parado ou rodando. Não existe janela.

O daemon abre uma exceção explícita, em `daemon/archive.go`: antes de checar o
rootfs, ele verifica se o destino cai dentro de um volume montado para escrita
e, nesse caso, permite. Volumes são montados **também para o container
parado**, e é isso que torna possível entregar os parâmetros antes do start,
sem corrida contra a automação lendo o arquivo.

### Por que não tmpfs para a área de trabalho

Um `tmpfs` do `HostConfig` é criado pelo runc **dentro do namespace de
montagem do container**. A API de archive resolve caminhos no rootfs do
graphdriver mais os volumes registrados, do lado do host. São dois universos: o
arquivo existe para a automação e não existe para o daemon, que responde
`404 Could not find the file /trabalho/... in container` com o container
rodando e o arquivo gravado.

Isto não é questão de momento. A primeira tentativa de correção foi colher cada
artefato no instante em que a automação o anuncia, em vez de ao final, por
suspeita de que o `tmpfs` fosse desmontado junto com o container. Não resolveu,
porque a causa nunca foi quando se colhe, e sim quem consegue ver.

O teto de tamanho, que é o motivo de o `tmpfs` existir, não se perde: um volume
do driver `local` com `driver_opts: {type: tmpfs, o: "size=100m,mode=1777"}`
monta o tmpfs em `/var/lib/docker/volumes/<nome>/_data`, no host, e o container
recebe um bind daquele caminho. Um tmpfs só, visível dos dois lados.

### Por que volume e não bind mount

O runner é ele mesmo um container, falando com o socket do daemon do host. Um
caminho de bind seria resolvido no host, não no filesystem do runner: o runner
não tem como oferecer um diretório seu. Volume é gerenciado pelo daemon e
dispensa tradução de caminho, o que mantém o executor igual rodando dentro ou
fora de container.

## Consequências

**A favor.** O que a automação recebe e o que ela entrega passam pelo mesmo
mecanismo, auditável e sem tocar no host. Parâmetro sensível não aparece em
`docker inspect`, ao contrário de variável de ambiente. O arquivo de parâmetros
é 0444 root:root em volume separado, então a automação, que roda como
`1000:1000`, lê e não reescreve, nem por acidente nem de propósito.

**Contra.** Duas chamadas de API a mais por execução para criar os volumes e
duas para removê-los. E dois recursos a mais que podem sobrar: volume órfão
ocupa espaço até alguém notar. O executor remove os dois no `finally` e, antes
de criar, apaga as sobras da tentativa anterior registrando WARNING. Sobra
silenciosa é o que transforma uma falha em outra, pior: um container em
`Created` que nunca foi removido bloqueia **toda** retentativa da mesma
execução com `409 Conflict` no nome, até a mensagem morrer na DLQ.

Também se perde uma simplicidade honesta: o adaptador agora carrega
conhecimento sobre o funcionamento interno da API de archive do Docker. É
conhecimento verdadeiro e necessário, mas é acoplamento a detalhe de
implementação de uma dependência, e uma mudança de comportamento do daemon
quebraria isto sem que nenhum teste unitário percebesse.

## Alternativas

**Parâmetros por variável de ambiente.** Simples e sem volume nenhum. Recusada
porque variável de ambiente aparece inteira em `docker inspect`, que qualquer
um com acesso ao socket roda, e parâmetro de execução pode conter dado pessoal.
Os segredos declarados no manifesto **já** viajam por ambiente, o que é uma
inconsistência conhecida: o segredo vem da plataforma e é auditado, o parâmetro
vem do formulário de quem executa.

**Parâmetros por stdin.** Fecha o problema sem volume e sem `docker inspect`.
Recusada porque mudaria o contrato do SDK, que hoje é só imprimir linhas no
stdout, para ler e consumir stdin na inicialização. Ver [0006](0006-protocolo-por-stdout.md).

**Raiz escrevível.** Resolveria a entrega dos parâmetros com uma linha a menos.
Recusada porque abre mão da garantia mais útil da postura de segurança: código
de terceiro que não consegue alterar a própria imagem em execução.

**Colher artefato por `docker cp` depois do fim.** Mesmo problema, já descrito:
`docker cp` usa a mesma API de archive.
