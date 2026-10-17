# 0003 - Um schema por serviço, sem chave estrangeira entre schemas

**Situação:** aceita · **Data:** 2026-10-17
**Implementa:** `hub_core/db/base.py`, `migrations/`

## Contexto

Quatro serviços compartilham uma instância de PostgreSQL. Isso é economia
deliberada, um banco gerenciado por serviço custaria quatro vezes mais e não
traria benefício no volume do projeto.

Mas compartilhar instância cria uma tentação forte: como as tabelas estão
todas ali, um `JOIN` entre domínios resolve qualquer consulta em um comando. E
no dia em que se decide extrair um serviço, descobre-se que metade das
consultas atravessa a fronteira.

## Decisão

Cada serviço tem o seu **schema** (`identity`, `catalog`, `execution`,
`runner`), mais um schema `plataforma` para infraestrutura (fila, artefatos,
auditoria).

Dentro de um schema existem chaves estrangeiras de verdade. **Entre schemas,
não.** A referência é lógica: `execution.execucao.automacao_id` aponta para
`catalog.automacao.id` sem constraint.

A regra é verificável, e é esse o ponto: se alguém escrever um `JOIN` entre
schemas, a migração que criaria a FK não tem o que referenciar, e o erro
aparece no CI, não seis meses depois.

Há uma regra complementar sobre o que **pode** ser copiado entre serviços:
**rótulo sim, estado não.** `execucao` guarda `automacao_nome` e
`usuario_nome` porque uma execução é um fato passado, se a automação for
renomeada em 2027, o relatório de 2026 tem de continuar dizendo o nome que ela
tinha. Copiar *estado* (se a automação está publicada) seria bug, porque
estado muda e a cópia envelheceria.

## Consequências

**A favor.** A fronteira é verificável, não apenas documentada. Extrair um
serviço para o seu próprio banco é mover um schema. A desnormalização de
rótulos dá correção histórica de graça.

**Contra.** Consultas que precisam de dado dos dois lados viram duas chamadas
HTTP compostas no portal, a ficha de uma automação faz uma ao catálogo e uma
ao motor. Mitigado com `asyncio.gather`, que paga o tempo da mais lenta e não
a soma. E a integridade referencial entre domínios passa a ser
responsabilidade da aplicação: um id órfão é possível.

Aceita-se o órfão conscientemente. O contrário, impedir o `identity-service`
de apagar uma unidade por causa de uma referência em outro domínio, é pior.

## Alternativas

- **Um banco por serviço.** Correto, e quatro vezes o custo. O caminho está
  aberto: cada schema vira um banco sem mudar código.
- **Schema único com prefixo de tabela.** Mesma disciplina, sem nada que a
  imponha. Prefixo é convenção; schema é mecanismo.
- **Uma linha de versão de migração por serviço.** Só se paga quando os bancos
  são de fato independentes. Com um banco, cinco linhas de versão produziriam
  combinações de estado que ninguém testou.
