"""Torna `plataforma.auditoria` imutável no nível do banco.

Escrita à mão: `autogenerate` compara tabelas, colunas e índices, e não vê
trigger nem função.

## Por que no banco, e não na aplicação

Um registro de auditoria que o próprio sistema pode reescrever não serve como
prova. Enquanto a garantia é "o código não faz `UPDATE` nessa tabela", ela vale
até o primeiro `UPDATE` escrito por engano, por um script de correção às
pressas, ou por um bug em um `ORM` que decide fazer *upsert*. A garantia tem de
sobreviver a um erro na aplicação, e o único lugar onde isso acontece é o banco.

## Por que trigger que levanta erro, e não `RULE ... DO INSTEAD NOTHING`

A receita tradicional é:

    CREATE RULE sem_update AS ON UPDATE TO auditoria DO INSTEAD NOTHING;

Funciona — e é a pior opção, porque o `UPDATE` **passa em silêncio**. O código
que tentou alterar o registro recebe sucesso e segue achando que alterou. Um
bug assim pode viver anos.

Com o trigger abaixo, a tentativa levanta exceção: a transação é desfeita e o
erro aparece no log e no teste. Falhar alto é o comportamento certo para uma
violação de regra que nunca deveria acontecer.

## Três gatilhos, não um

`UPDATE` e `DELETE` são barrados por um trigger de **statement** — que dispara
mesmo quando o comando não casa nenhuma linha, deixando a intenção clara em vez
de "0 linhas afetadas".

`TRUNCATE` precisa do seu próprio: ele não dispara triggers de linha nem de
statement de UPDATE/DELETE, e é justamente o comando que alguém usaria para
limpar a tabela.

## O que isto **não** garante

O dono da tabela ainda pode `DROP TRIGGER` e depois alterar o que quiser. Não
há defesa contra quem tem DDL no banco — a defesa aí é organizacional: a
aplicação se conecta com um papel sem privilégio de DDL, e o papel dono fica
com a equipe de banco. Em produção isso é um `GRANT`; aqui, com um único papel
no docker-compose, está registrado como limite conhecido.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


FUNCAO_RECUSAR = """
CREATE OR REPLACE FUNCTION plataforma.recusar_alteracao_de_auditoria()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION
        'plataforma.auditoria e somente insercao: % nao e permitido', TG_OP
        USING ERRCODE = 'restrict_violation',
              HINT = 'a trilha de auditoria nao pode ser alterada nem apagada';
END;
$$;
"""

COMENTARIO_FUNCAO = """
COMMENT ON FUNCTION plataforma.recusar_alteracao_de_auditoria() IS
'Barra UPDATE, DELETE e TRUNCATE em plataforma.auditoria. Ver migracao 0002.';
"""

TRIGGER_UPDATE_DELETE = """
CREATE TRIGGER auditoria_sem_alteracao
BEFORE UPDATE OR DELETE ON plataforma.auditoria
FOR EACH STATEMENT
EXECUTE FUNCTION plataforma.recusar_alteracao_de_auditoria();
"""

TRIGGER_TRUNCATE = """
CREATE TRIGGER auditoria_sem_truncate
BEFORE TRUNCATE ON plataforma.auditoria
FOR EACH STATEMENT
EXECUTE FUNCTION plataforma.recusar_alteracao_de_auditoria();
"""

COMENTARIO_TABELA = """
COMMENT ON TABLE plataforma.auditoria IS
'Trilha de auditoria. Somente insercao, garantida por trigger (migracao 0002).
Gravada na mesma transacao da acao auditada: ou as duas coisas acontecem, ou
nenhuma. Nao ha FK para identity.usuario de proposito — o registro sobrevive a
exclusao do usuario auditado, e guarda nome e CPF como estavam no momento da
acao.';
"""


def upgrade() -> None:
    op.execute(FUNCAO_RECUSAR)
    op.execute(COMENTARIO_FUNCAO)
    op.execute(TRIGGER_UPDATE_DELETE)
    op.execute(TRIGGER_TRUNCATE)
    op.execute(COMENTARIO_TABELA)


def downgrade() -> None:
    # A ordem inversa importa: a função não pode ser removida enquanto os
    # triggers a referenciam.
    op.execute("DROP TRIGGER IF EXISTS auditoria_sem_truncate ON plataforma.auditoria")
    op.execute("DROP TRIGGER IF EXISTS auditoria_sem_alteracao ON plataforma.auditoria")
    op.execute("DROP FUNCTION IF EXISTS plataforma.recusar_alteracao_de_auditoria()")
