# 0009 - Parser de cron próprio, com o subconjunto POSIX

**Situação:** aceita · **Data:** 2026-10-17
**Implementa:** `hub_execution/agenda.py`

## Contexto

O agendador precisa interpretar expressões cron e calcular a próxima
ocorrência. `croniter` e `apscheduler` fazem isso bem.

## Decisão

Implementar, com o subconjunto necessário: cinco campos, com `*`, número,
faixa, lista e passo. Sem `@yearly`, sem segundos, sem `L`/`#` de cron
estendido. São cerca de 120 linhas sem dependência.

Dois motivos:

1. **o subconjunto é pequeno** e cabe em um arquivo que se lê de uma vez;
2. **o comportamento fica explícito e testável**, em especial a semântica de
   dia-do-mês × dia-da-semana, que é a parte do cron que quase todo mundo
   implementa errado.

Essa semântica merece destaque porque é contraintuitiva e silenciosa: quando
**ambos** os campos de dia estão restritos, a relação é **OU**, não **E**.
`0 0 1 * 1` dispara no dia 1º do mês *e também* em toda segunda-feira, não
apenas no dia 1º que cair numa segunda. É assim desde o cron original do Unix
e está no POSIX. Implementar como **E** produz um agendamento que quase nunca
dispara, e o erro passa despercebido por meses, porque não gera falha, só
silêncio. O teste `test_cron_dia_do_mes_ou_dia_da_semana` existe para isso.

Outras duas decisões:

- **As expressões são interpretadas em `America/Sao_Paulo`, não em UTC.** Quem
  agenda "0 6 * * *" quer seis da manhã no horário de quem vai receber o
  relatório, não 3h porque o servidor está em UTC. O cálculo usa `zoneinfo`
  sobre datas conscientes de fuso, e não aritmética de offset, o Brasil não
  adota horário de verão desde 2019, mas a implementação não depende disso
  continuar valendo.
- **A busca avança minuto a minuto, pulando dias e horas que não casam.** É
  mais simples de conferir do que o cálculo campo a campo, e o custo é
  irrelevante para algo que roda uma vez por minuto. A janela é de 4 anos,
  dimensionada por `0 0 29 2 *`, a expressão válida com o maior intervalo
  possível entre ocorrências.

## Consequências

**A favor.** Nenhuma dependência. O comportamento está no repositório e
coberto por testes, incluindo os casos que a maioria das implementações erra.

**Contra.** É código a manter, e um parser de cron tem mais casos de borda do
que parece, a faixa do dia-da-semana precisando aceitar `7` como domingo foi
um bug encontrado só na validação. Se o projeto precisasse de `@reboot`,
segundos ou cron estendido, a decisão correta seria trocar por `croniter`:
neste ponto o custo de manter superaria o de depender.

## Alternativas

- **`croniter`.** Testado e completo. Seria a escolha certa se o subconjunto
  necessário fosse maior.
- **`APScheduler`.** Traz um agendador inteiro, com o seu próprio modelo de
  armazenamento de jobs, e o projeto já tem fila, lock e persistência. Seria
  um segundo agendador dentro do primeiro.
- **Delegar ao Kubernetes (`CronJob`).** O desenho da arquitetura-alvo. Não
  funciona no free tier e tiraria do produto a visibilidade dos agendamentos,
  que hoje aparecem no painel com a próxima ocorrência calculada.
