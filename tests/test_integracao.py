"""Testes que exigem um PostgreSQL de verdade.

São poucos e caros, e cada um prova algo que **só** se prova contra o banco:
o `SKIP LOCKED` da fila sob concorrência, a imutabilidade da auditoria imposta
por trigger, e o controle de admissão com disparos simultâneos.

Pulados automaticamente sem `DATABASE_URL` — ver `conftest.py`.

    make up                                   # sobe o PostgreSQL
    export DATABASE_URL=postgresql+asyncpg://orbita:orbita_dev@localhost:55432/orbita
    alembic upgrade head
    pytest -m integracao
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text

from hub_core import db
from hub_core.broker.postgres import BrokerPostgres
from hub_core.executor import (
    ExecutorSimulado,
    LimitesDeRecurso,
    PedidoDeExecucao,
    StatusExecucao,
)
from hub_core.settings import Settings
from hub_core.storage.postgres import ArtefatoStorePostgres

pytestmark = pytest.mark.integracao

FILA = "execucoes.padrao"


def _broker(settings: Settings, *, visibilidade: int = 300, tentativas: int = 3) -> BrokerPostgres:
    return BrokerPostgres(
        db.fabrica_de_sessoes(),
        visibilidade_segundos=visibilidade,
        max_tentativas=tentativas,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Fila em PostgreSQL
# ─────────────────────────────────────────────────────────────────────────────


async def test_publicar_consumir_confirmar(settings: Settings, limpar_fila: None) -> None:
    del limpar_fila
    broker = _broker(settings)

    await broker.publicar(FILA, {"execucao_id": "abc", "n": 1})
    assert (await broker.profundidade(FILA))["pendente"] == 1

    mensagens = await broker.consumir(FILA, limite=5)
    assert len(mensagens) == 1
    assert mensagens[0].corpo["n"] == 1
    assert mensagens[0].tentativas == 1

    profundidade = await broker.profundidade(FILA)
    assert profundidade["pendente"] == 0
    assert profundidade["processando"] == 1

    await broker.confirmar(mensagens[0])
    assert (await broker.profundidade(FILA))["concluida"] == 1


async def test_skip_locked_nunca_entrega_a_mesma_mensagem_duas_vezes(
    settings: Settings, limpar_fila: None
) -> None:
    """A propriedade central da fila em SQL, e a única que de fato se promete.

    Oito consumidores concorrentes sobre dezesseis mensagens: cada um pega um
    conjunto **disjunto**, sem esperar pelos outros. É para isto que o
    PostgreSQL 9.5 introduziu o `SKIP LOCKED`, e é a razão de a fila poder
    morar em uma tabela.

    O que este teste deliberadamente **não** afirma é que uma rodada esvazia a
    fila. Com `SKIP LOCKED`, o `LIMIT` é aplicado à varredura e não às linhas
    efetivamente travadas: um consumidor que pede 2 e encontra uma delas já
    travada devolve 1. Em uma rodada de oito consumidores pedindo 2, costumam
    vir 8 mensagens, não 16 — comportamento correto, e a razão de
    `BrokerPostgres.consumir` prometer "até `limite`".

    Sem `SKIP LOCKED` (só com `FOR UPDATE`) os consumidores se serializariam;
    sem nenhum dos dois, haveria entrega duplicada e o teste falharia.
    """
    del limpar_fila
    broker = _broker(settings)

    total = 16
    for indice in range(total):
        await broker.publicar(FILA, {"execucao_id": f"e{indice}", "indice": indice})

    vistos: list[str] = []
    indices: list[int] = []

    # Rodadas até esvaziar: cada rodada traz um lote possivelmente parcial.
    for _ in range(12):
        lotes = await asyncio.gather(*[broker.consumir(FILA, limite=2) for _ in range(8)])
        colhidas = [m for lote in lotes for m in lote]
        if not colhidas:
            break
        vistos.extend(str(m.id) for m in colhidas)
        indices.extend(m.corpo["indice"] for m in colhidas)
        for mensagem in colhidas:
            await broker.confirmar(mensagem)

    assert len(set(vistos)) == len(vistos), (
        "a mesma mensagem foi entregue a dois consumidores — "
        "é o que o SKIP LOCKED existe para impedir"
    )
    assert sorted(indices) == list(range(total)), "alguma mensagem ficou para trás"


async def test_lote_parcial_sob_concorrencia(settings: Settings, limpar_fila: None) -> None:
    """Documenta em teste o comportamento do `LIMIT` com `SKIP LOCKED`.

    Existe para que ninguém "conserte" `consumir` achando que ele deveria
    sempre encher o lote — e acabe removendo o `SKIP LOCKED` para consegui-lo.
    """
    del limpar_fila
    broker = _broker(settings)

    for indice in range(10):
        await broker.publicar(FILA, {"execucao_id": f"p{indice}"})

    lotes = await asyncio.gather(*[broker.consumir(FILA, limite=5) for _ in range(2)])
    tamanhos = sorted(len(lote) for lote in lotes)

    # Pode vir [5, 5] sem contenção real, ou menos com ela. O que não pode é
    # repetir mensagem entre os lotes.
    ids_a = {str(m.id) for m in lotes[0]}
    ids_b = {str(m.id) for m in lotes[1]}
    assert not (ids_a & ids_b), "lotes concorrentes se sobrepuseram"
    assert sum(tamanhos) <= 10


async def test_prioridade_e_respeitada(settings: Settings, limpar_fila: None) -> None:
    del limpar_fila
    broker = _broker(settings)

    await broker.publicar(FILA, {"execucao_id": "baixa"}, prioridade=1)
    await broker.publicar(FILA, {"execucao_id": "alta"}, prioridade=9)
    await broker.publicar(FILA, {"execucao_id": "media"}, prioridade=5)

    ordem = [m.corpo["execucao_id"] for m in await broker.consumir(FILA, limite=3)]
    assert ordem == ["alta", "media", "baixa"]


async def test_atraso_torna_a_mensagem_invisivel(settings: Settings, limpar_fila: None) -> None:
    """`atraso_segundos` é o que viabiliza agendamento e backoff sem componente
    separado: no PostgreSQL vira `disponivel_em` no futuro."""
    del limpar_fila
    broker = _broker(settings)

    await broker.publicar(FILA, {"execucao_id": "futura"}, atraso_segundos=600)
    assert await broker.consumir(FILA, limite=5) == []
    assert (await broker.profundidade(FILA))["pendente"] == 1


async def test_rejeicao_reenfileira_com_backoff(settings: Settings, limpar_fila: None) -> None:
    del limpar_fila
    broker = _broker(settings)

    await broker.publicar(FILA, {"execucao_id": "falha"})
    mensagem = (await broker.consumir(FILA))[0]

    await broker.rejeitar(mensagem, motivo="erro de teste")

    # Voltou para pendente, mas com `disponivel_em` no futuro (backoff de 5 s).
    assert (await broker.profundidade(FILA))["pendente"] == 1
    assert await broker.consumir(FILA) == [], "o backoff deve segurar a reentrega"

    async with db.fabrica_de_sessoes()() as sessao:
        linha = (
            (
                await sessao.execute(
                    text("SELECT tentativas, ultimo_erro FROM plataforma.fila_mensagens LIMIT 1")
                )
            )
            .mappings()
            .one()
        )
    assert linha["tentativas"] == 1
    assert linha["ultimo_erro"] == "erro de teste"


async def test_mensagem_vai_para_a_dlq_apos_o_teto(settings: Settings, limpar_fila: None) -> None:
    """Esgotadas as tentativas, a mensagem morre em vez de rodar para sempre."""
    del limpar_fila
    broker = _broker(settings, tentativas=2)

    await broker.publicar(FILA, {"execucao_id": "condenada"}, max_tentativas=2)

    for _ in range(2):
        # Zera o backoff para o teste não precisar esperar.
        async with db.fabrica_de_sessoes()() as sessao:
            await sessao.execute(text("UPDATE plataforma.fila_mensagens SET disponivel_em = now()"))
            await sessao.commit()
        mensagens = await broker.consumir(FILA)
        if not mensagens:
            break
        await broker.rejeitar(mensagens[0], motivo="falha persistente")

    profundidade = await broker.profundidade(FILA)
    assert profundidade["morta"] == 1
    assert profundidade["pendente"] == 0

    mortas = await broker.listar_mortas()
    assert len(mortas) == 1
    assert mortas[0]["ultimo_erro"] == "falha persistente"

    # E pode ser revivida pela área administrativa, com as tentativas zeradas.
    assert await broker.reviver(mortas[0]["id"])
    assert (await broker.profundidade(FILA))["pendente"] == 1


async def test_visibility_timeout_devolve_mensagem_de_worker_morto(
    settings: Settings, limpar_fila: None
) -> None:
    """A rede de segurança para o worker que cai no meio do trabalho.

    O equivalente em PostgreSQL ao *redelivery* do RabbitMQ: a mensagem fica
    invisível por um prazo e volta se não houver `ack`.
    """
    del limpar_fila
    broker = _broker(settings, visibilidade=0)  # expira imediatamente

    await broker.publicar(FILA, {"execucao_id": "orfa"})
    mensagens = await broker.consumir(FILA)
    assert len(mensagens) == 1  # consumida e nunca confirmada

    recuperadas = await broker.recuperar_expiradas()
    assert recuperadas == 1
    assert (await broker.profundidade(FILA))["pendente"] == 1


async def test_limpeza_respeita_a_retencao(settings: Settings, limpar_fila: None) -> None:
    """O `ack` não apaga: a limpeza é um job separado, com retenção explícita."""
    del limpar_fila
    broker = _broker(settings)

    await broker.publicar(FILA, {"execucao_id": "x"})
    await broker.confirmar((await broker.consumir(FILA))[0])

    assert await broker.limpar_concluidas(dias=30) == 0, "recente demais para apagar"
    assert await broker.limpar_concluidas(dias=0) == 1


# ─────────────────────────────────────────────────────────────────────────────
# Auditoria imutável
# ─────────────────────────────────────────────────────────────────────────────


async def test_auditoria_recusa_update_delete_e_truncate(banco: None) -> None:
    """A garantia tem de sobreviver a um bug na aplicação — por isso é no banco.

    Observe que o teste roda com o **dono** da tabela: o trigger vale mesmo
    para quem tem todos os privilégios, que é o ponto.
    """
    del banco
    from sqlalchemy.exc import DBAPIError

    async with db.fabrica_de_sessoes()() as sessao:
        await sessao.execute(
            text(
                "INSERT INTO plataforma.auditoria (acao, recurso, ator_nome) "
                "VALUES ('teste.imutabilidade', 'r1', 'Teste')"
            )
        )
        await sessao.commit()

    for comando in (
        "UPDATE plataforma.auditoria SET acao = 'adulterado'",
        "DELETE FROM plataforma.auditoria",
        "TRUNCATE plataforma.auditoria",
    ):
        async with db.fabrica_de_sessoes()() as sessao:
            with pytest.raises(DBAPIError) as erro:
                await sessao.execute(text(comando))
            assert "somente insercao" in str(erro.value)
            await sessao.rollback()

    async with db.fabrica_de_sessoes()() as sessao:
        intactos = (
            await sessao.execute(
                text("SELECT count(*) FROM plataforma.auditoria WHERE acao = 'teste.imutabilidade'")
            )
        ).scalar_one()
    assert intactos >= 1


# ─────────────────────────────────────────────────────────────────────────────
# Armazenamento de artefatos
# ─────────────────────────────────────────────────────────────────────────────


async def test_artefato_em_postgres_respeita_o_teto(settings: Settings, banco: None) -> None:
    """O teto não é decorativo: o plano gratuito do Neon dá 0,5 GB para tudo."""
    del banco
    from hub_core.errors import EntradaInvalida

    store = ArtefatoStorePostgres(db.fabrica_de_sessoes(), tamanho_maximo_bytes=1024)

    meta = await store.salvar("teste/pequeno.txt", b"ok" * 100, tipo_conteudo="text/plain")
    assert meta.tamanho_bytes == 200
    assert await store.ler("teste/pequeno.txt") == b"ok" * 100

    with pytest.raises(EntradaInvalida):
        await store.salvar("teste/grande.bin", b"x" * 2048)

    assert await store.apagar("teste/pequeno.txt")
    assert not await store.apagar("teste/pequeno.txt")


async def test_artefato_recusa_caminho_que_escapa(settings: Settings, banco: None) -> None:
    """O nome do artefato vem da automação, que é código de terceiro."""
    del banco
    from hub_core.errors import EntradaInvalida

    store = ArtefatoStorePostgres(db.fabrica_de_sessoes())
    for caminho in ("../../etc/passwd", "a/../../b", "c:\\windows\\system32"):
        with pytest.raises(EntradaInvalida):
            await store.salvar(caminho, b"x")


async def test_stream_de_artefato_vem_em_pedacos(settings: Settings, banco: None) -> None:
    """O `bytea` é paginado em SQL: o processo nunca carrega o arquivo inteiro."""
    del banco
    store = ArtefatoStorePostgres(db.fabrica_de_sessoes())
    conteudo = bytes(range(256)) * 40  # 10 KB

    await store.salvar("teste/stream.bin", conteudo)

    pedacos = [p async for p in store.stream("teste/stream.bin", pedaco_bytes=1024)]
    assert len(pedacos) > 1, "deveria vir em mais de um pedaço"
    assert b"".join(pedacos) == conteudo

    await store.apagar("teste/stream.bin")


# ─────────────────────────────────────────────────────────────────────────────
# Lock distribuído
# ─────────────────────────────────────────────────────────────────────────────


async def test_advisory_lock_e_mutuamente_exclusivo(settings: Settings, banco: None) -> None:
    """É o que impede o agendamento das 6h de disparar uma vez por réplica."""
    del banco
    from hub_core.locks.postgres import LockPostgres

    primeiro = LockPostgres(db.engine())
    segundo = LockPostgres(db.engine())

    try:
        assert await primeiro.adquirir("teste:exclusao")
        assert not await segundo.adquirir("teste:exclusao"), "dois donos do mesmo lock"

        await primeiro.liberar("teste:exclusao")
        assert await segundo.adquirir("teste:exclusao"), "não liberou"
    finally:
        await primeiro.encerrar()
        await segundo.encerrar()


async def test_locks_de_nomes_diferentes_nao_se_bloqueiam(settings: Settings, banco: None) -> None:
    """Disparos de automações diferentes não devem esperar um pelo outro."""
    del banco
    from hub_core.locks.postgres import LockPostgres

    lock = LockPostgres(db.engine())
    try:
        assert await lock.adquirir("admissao:automacao-a")
        assert await lock.adquirir("admissao:automacao-b")
    finally:
        await lock.encerrar()


# ─────────────────────────────────────────────────────────────────────────────
# Executor simulado
# ─────────────────────────────────────────────────────────────────────────────


async def test_executor_simulado_reproduz_o_roteiro() -> None:
    """Não precisa de banco, mas depende do executor completo — fica aqui junto."""
    executor = ExecutorSimulado(acelerar=True)
    registradas: list[tuple[str, str]] = []

    async def registrar(nivel: str, mensagem: str) -> None:
        registradas.append((nivel, mensagem))

    pedido = PedidoDeExecucao(
        execucao_id="11111111-1111-4111-8111-111111111111",
        automacao_slug="teste",
        imagem="local/teste",
        limites=LimitesDeRecurso(),
        roteiro_simulado=[
            {"espera_ms": 0, "log": "primeiro passo"},
            {"espera_ms": 0, "metrica": {"linhas": 42}},
            {
                "espera_ms": 0,
                "artefato": {
                    "caminho": "saida.csv",
                    "formato": "csv",
                    "linhas": 5,
                    "colunas": ["uf", "vazao_m3_s"],
                },
            },
            {"espera_ms": 0, "resumo": "pronto"},
        ],
    )

    resultado = await executor.executar(pedido, registrar)

    assert resultado.status is StatusExecucao.SUCESSO
    assert resultado.resumo == "pronto"
    assert resultado.metricas == {"linhas": 42.0}
    assert len(resultado.artefatos) == 1

    artefato = resultado.artefatos[0]
    assert artefato.caminho_relativo == "saida.csv"
    assert artefato.conteudo.startswith(b"\xef\xbb\xbf"), "CSV com BOM, para o Excel"
    primeira_linha = artefato.conteudo.decode("utf-8-sig").splitlines()[0]
    assert primeira_linha == "uf;vazao_m3_s"

    assert resultado.consumo["simulado"] is True, "a marcação é obrigatória"
    assert any("primeiro passo" in m for _, m in registradas)


async def test_executor_simulado_e_deterministico_por_execucao() -> None:
    """Mesma execução, mesmo artefato — é o que torna o teste estável."""
    executor = ExecutorSimulado(acelerar=True)

    async def nada(_n: str, _m: str) -> None:
        return None

    def pedido_para(execucao_id: str) -> PedidoDeExecucao:
        return PedidoDeExecucao(
            execucao_id=execucao_id,
            automacao_slug="teste",
            imagem="local/teste",
            roteiro_simulado=[
                {
                    "espera_ms": 0,
                    "artefato": {
                        "caminho": "a.csv",
                        "formato": "csv",
                        "linhas": 3,
                        "colunas": ["uf"],
                    },
                }
            ],
        )

    mesmo_id = "22222222-2222-4222-8222-222222222222"
    outro_id = "33333333-3333-4333-8333-333333333333"

    um = (await executor.executar(pedido_para(mesmo_id), nada)).artefatos[0].conteudo
    dois = (await executor.executar(pedido_para(mesmo_id), nada)).artefatos[0].conteudo
    tres = (await executor.executar(pedido_para(outro_id), nada)).artefatos[0].conteudo

    assert um == dois, "o mesmo id deve produzir o mesmo artefato"
    assert um != tres, "ids diferentes devem produzir dados diferentes"


async def test_executor_simulado_reporta_falha_sem_levantar() -> None:
    """Falha de automação é um `ResultadoExecucao`, não uma exceção.

    A distinção é o que o worker usa para decidir entre `ack` e `nack` — ver
    o docstring de `hub_runner.worker`.
    """
    executor = ExecutorSimulado(acelerar=True)

    async def nada(_n: str, _m: str) -> None:
        return None

    resultado = await executor.executar(
        PedidoDeExecucao(
            execucao_id="44444444-4444-4444-8444-444444444444",
            automacao_slug="teste",
            imagem="local/teste",
            roteiro_simulado=[{"espera_ms": 0, "falha": "limite excedido", "codigo_saida": 2}],
        ),
        nada,
    )

    assert resultado.status is StatusExecucao.FALHA
    assert resultado.codigo_saida == 2
    assert resultado.erro == "limite excedido"
