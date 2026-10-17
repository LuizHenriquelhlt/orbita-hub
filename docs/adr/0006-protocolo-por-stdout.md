# 0006 — A automação fala com a plataforma pelo stdout

**Situação:** aceita · **Data:** 2026-10-17
**Implementa:** `hub_core/executor/base.py`, `sdk/hub_sdk/`

## Contexto

Uma automação é código de terceiro, possivelmente em outra linguagem. Precisa
emitir log, publicar métrica, declarar o arquivo que produziu e dizer como
terminou — e a plataforma precisa receber isso **enquanto** ela roda, não no
fim.

A solução óbvia é um SDK que faz chamadas HTTP de volta ao
`execution-service`. Foi a primeira escolhida, e tem dois problemas que só
aparecem depois:

1. exige que **todo container** tenha rota de rede até o serviço e credencial
   para autenticar — ou seja, nenhuma automação pode rodar com a rede
   desligada;
2. exige um **SDK por linguagem** antes de a linguagem ser suportada.

## Decisão

A automação **imprime** no stdout. Linhas com o prefixo `@@orbita ` seguido de
um JSON são comandos de protocolo; qualquer outra linha é log em nível INFO.

```
@@orbita {"tipo": "log", "nivel": "WARNING", "mensagem": "3 registros sem CNPJ"}
@@orbita {"tipo": "metrica", "nome": "linhas_processadas", "valor": 1420}
@@orbita {"tipo": "artefato", "caminho": "relatorio.xlsx"}
@@orbita {"tipo": "resumo", "mensagem": "1420 outorgas em 8 regiões"}
```

Três consequências boas:

1. **funciona nos três executores sem adaptação** — `docker logs`, o stdout de
   um subprocesso e a saída do simulador são a mesma coisa: linhas de texto;
2. **não precisa de SDK.** Um `echo` em shell script é um cliente completo. O
   `hub_sdk` é conveniência tipada, não requisito;
3. **o container não precisa de rede nem de credencial**, o que permite rodá-lo
   com `--network none` — o padrão da plataforma.

O parser é tolerante: linha com o prefixo mas JSON inválido vira log, não
erro. Uma automação não deve falhar porque emitiu uma linha de telemetria
malformada — mas o problema precisa aparecer, então a linha crua fica no log.

## Consequências

**A favor.** Qualquer linguagem que saiba imprimir é suportada. O isolamento
de rede fica viável. O log vem na ordem em que foi produzido, de graça.

**Contra.** O canal é **unidirecional**: a automação não consulta a plataforma
durante a execução. Para o conjunto de automações batch que a plataforma
atende, nunca foi necessário; para uma automação interativa, seria um limite
real.

Há também uma armadilha do lado de quem escreve a automação: **sem `flush`, o
Python armazena a saída em buffer quando o stdout não é um terminal** — e é
exatamente o caso dentro de um container. O log apareceria todo de uma vez no
fim, destruindo o acompanhamento ao vivo. É o erro mais comum ao escrever a
primeira automação, e a razão de o `hub_sdk` existir: ele faz `flush=True` em
toda emissão.

## Alternativas

- **SDK com callback HTTP.** O desenho da arquitetura-alvo original. Exige
  rede e credencial em todo container.
- **Arquivo de log em volume compartilhado.** Exige montar volume do host no
  container, que é justamente o que mantém o host fora do alcance da
  automação.
- **gRPC ou socket Unix.** Mais eficiente e traz dependência para dentro do
  container da automação — o oposto do objetivo.
