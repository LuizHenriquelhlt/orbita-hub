"""O worker: consome a fila, executa e reporta.

## O laço

    para cada fila, da mais prioritária para a menos:
        se há vaga:  consome, dispara o processamento, segue
    nada em nenhuma fila:  dorme o intervalo de poll

A ordem das filas é a prioridade entre elas: `interativa` antes de `padrao`
antes de `lote`. É *starvation* por desenho — um relatório mensal pesado nunca
atrasa a automação que alguém está esperando na tela. Em troca, uma fila de
lote pode esperar muito quando há carga interativa constante, o que é o
comportamento desejado para este domínio.

A concorrência é limitada por um semáforo. Sem ele, uma fila com 200 mensagens
geraria 200 execuções simultâneas e o container morreria por falta de memória
antes da décima.

## Ack e nack: quem decide o quê

É a parte sutil. Há **dois** tipos de falha, e tratá-los igual é o erro:

* **a automação falhou** (código de saída ≠ 0, timeout, erro no script). Isso é
  um resultado, não um problema de entrega: a execução é finalizada com status
  de falha e a mensagem recebe **ack**. Dar nack reexecutaria uma automação
  que falha deterministicamente, três vezes, para falhar as três;
* **o runner falhou** (não conseguiu puxar a imagem, perdeu o Docker, o
  `execution-service` não respondeu). A automação não teve a sua chance: a
  mensagem recebe **nack** e volta à fila com backoff, para outro runner — ou
  este, mais tarde — tentar de novo.

Em uma frase: *nack é para falha de infraestrutura; ack é para resultado de
automação, inclusive resultado ruim*.
"""

from __future__ import annotations

import asyncio
from typing import Any

from hub_core.auth.tokens import emitir_token_de_servico
from hub_core.broker import FILA_INTERATIVA, FILA_LOTE, FILA_PADRAO, Broker, Mensagem
from hub_core.executor import (
    Executor,
    LimitesDeRecurso,
    PedidoDeExecucao,
    ResultadoExecucao,
    StatusExecucao,
)
from hub_core.logging import definir_trace_id, obter_logger
from hub_core.settings import Settings
from hub_core.storage import ArtefatoStore
from hub_runner.coletor import ColetorDeLogs

log = obter_logger(__name__)

# Ordem de atendimento. Ver docstring do módulo.
ORDEM_DAS_FILAS = (FILA_INTERATIVA, FILA_PADRAO, FILA_LOTE)


class FalhaDeInfraestrutura(Exception):
    """Levantada quando o runner não conseguiu dar à automação a sua chance.

    Distingue-se de uma automação que rodou e falhou: esta exceção leva a
    `nack` (nova tentativa), e o resultado de falha da automação leva a `ack`.
    """


class Worker:
    def __init__(
        self,
        *,
        broker: Broker,
        executor: Executor,
        storage: ArtefatoStore,
        cliente_execution: Any,
        settings: Settings,
        max_concorrentes: int = 2,
    ) -> None:
        self._broker = broker
        self._executor = executor
        self._storage = storage
        self._cliente = cliente_execution
        self._settings = settings
        self._vagas = asyncio.Semaphore(max_concorrentes)
        self._max_concorrentes = max_concorrentes
        self._em_execucao: dict[str, asyncio.Task[None]] = {}
        self.processadas = 0

    # ── Laço principal ───────────────────────────────────────────────────────

    async def laco(self, parar: asyncio.Event) -> None:
        log.info(
            "worker.iniciado",
            executor=self._executor.nome,
            max_concorrentes=self._max_concorrentes,
            filas=list(ORDEM_DAS_FILAS),
        )

        while not parar.is_set():
            try:
                colheu = await self._uma_volta()
            except asyncio.CancelledError:
                raise
            except Exception:
                # O laço não pode morrer: se ele cair, a fila para de ser
                # consumida e nada avisa.
                log.error("worker.erro_no_laco", exc_info=True)
                colheu = False

            if not colheu:
                try:
                    await asyncio.wait_for(
                        parar.wait(), timeout=self._settings.fila_intervalo_poll_segundos
                    )
                except TimeoutError:
                    continue

        await self._aguardar_em_execucao()
        log.info("worker.encerrado", processadas=self.processadas)

    async def _uma_volta(self) -> bool:
        """Tenta pegar uma mensagem. Devolve `True` se pegou alguma."""
        if self._vagas.locked():
            # Sem vaga: não retira mensagem da fila. Retirar e segurar em
            # memória tornaria a mensagem invisível a outros runners sem estar
            # sendo processada — desperdício de capacidade disfarçado de
            # trabalho em andamento.
            return False

        for fila in ORDEM_DAS_FILAS:
            mensagens = await self._broker.consumir(fila, limite=1)
            if not mensagens:
                continue

            mensagem = mensagens[0]
            await self._vagas.acquire()
            tarefa = asyncio.create_task(
                self._processar_e_liberar(mensagem),
                name=f"exec-{mensagem.corpo.get('execucao_id', '?')[:8]}",
            )
            self._em_execucao[str(mensagem.id)] = tarefa
            return True

        return False

    async def _processar_e_liberar(self, mensagem: Mensagem) -> None:
        try:
            await self._processar(mensagem)
        finally:
            self._vagas.release()
            self._em_execucao.pop(str(mensagem.id), None)
            self.processadas += 1

    async def _aguardar_em_execucao(self) -> None:
        """No encerramento, espera as execuções em andamento terminarem.

        Com teto: o orquestrador mata o processo depois do `terminationGrace
        Period`, e é melhor encerrar por conta própria do que ser morto no
        meio de um `finalizar`. As que não terminarem voltam à fila pelo
        *visibility timeout*.
        """
        if not self._em_execucao:
            return
        log.info("worker.aguardando_execucoes", total=len(self._em_execucao))
        await asyncio.wait(list(self._em_execucao.values()), timeout=30)

    # ── Processamento de uma mensagem ────────────────────────────────────────

    async def _processar(self, mensagem: Mensagem) -> None:
        corpo = mensagem.corpo
        execucao_id = str(corpo.get("execucao_id") or "")

        if not execucao_id:
            # Mensagem sem id de execução não tem como ser reportada nem
            # corrigida por nova tentativa. Vai direto para a DLQ.
            log.error("worker.mensagem_sem_execucao", mensagem_id=str(mensagem.id))
            await self._broker.rejeitar(mensagem, motivo="mensagem sem execucao_id")
            return

        definir_trace_id(mensagem.metadados.get("trace_id") or execucao_id[:16])

        try:
            await self._reportar_inicio(execucao_id, mensagem.tentativas)
        except FalhaDeInfraestrutura as erro:
            # Não dá para começar sem registrar o início: sem isso a interface
            # mostraria "na fila" durante toda a execução.
            await self._broker.rejeitar(mensagem, motivo=str(erro))
            definir_trace_id(None)
            return

        pedido = self._montar_pedido(execucao_id, corpo)

        async with ColetorDeLogs(self._cliente, execucao_id) as coletor:
            try:
                resultado = await self._executor.executar(pedido, coletor.registrar)
            except Exception as erro:
                # Exceção que escapa do executor é falha **dele**, não da
                # automação — o contrato em `Executor.executar` diz que falha
                # de automação volta como `ResultadoExecucao`. Então: nack.
                log.error("worker.executor_falhou", execucao_id=execucao_id, exc_info=True)
                await coletor.registrar("ERROR", f"falha do executor: {erro}")
                await self._broker.rejeitar(mensagem, motivo=f"executor falhou: {erro}")
                definir_trace_id(None)
                return

            artefatos = await self._salvar_artefatos(execucao_id, resultado, coletor)

        try:
            await self._reportar_finalizacao(execucao_id, resultado, artefatos)
        except FalhaDeInfraestrutura as erro:
            # A automação rodou, mas o resultado não chegou. Nack: a nova
            # tentativa a reexecuta, o que é aceitável porque a plataforma
            # exige que automação seja idempotente (ver contrato do SDK), e a
            # alternativa — perder o resultado — é pior.
            log.error("worker.finalizacao_nao_entregue", execucao_id=execucao_id)
            await self._broker.rejeitar(mensagem, motivo=str(erro))
            definir_trace_id(None)
            return

        # A automação teve a sua chance e produziu um resultado — inclusive se
        # o resultado foi falha. Ack.
        await self._broker.confirmar(mensagem)

        log.info(
            "worker.concluiu",
            execucao_id=execucao_id,
            status=resultado.status.value,
            artefatos=len(artefatos),
            logs_enviados=coletor.total_enviadas,
        )
        definir_trace_id(None)

    def _montar_pedido(self, execucao_id: str, corpo: dict[str, Any]) -> PedidoDeExecucao:
        limites_brutos = corpo.get("limites") or {}
        limites = LimitesDeRecurso(
            cpus=float(limites_brutos.get("cpus", 0.5)),
            memoria_mb=int(limites_brutos.get("memoria_mb", 512)),
            timeout_segundos=int(
                limites_brutos.get(
                    "timeout_segundos", self._settings.execucao_timeout_padrao_segundos
                )
            ),
            disco_mb=int(limites_brutos.get("disco_mb", 256)),
        )

        return PedidoDeExecucao(
            execucao_id=execucao_id,
            automacao_slug=str(corpo.get("automacao_slug") or ""),
            imagem=str(corpo.get("imagem") or ""),
            entrypoint=list(corpo.get("entrypoint") or []),
            parametros=dict(corpo.get("parametros") or {}),
            variaveis_ambiente=self._resolver_segredos(
                str(corpo.get("automacao_slug") or ""),
                corpo.get("variaveis_ambiente") or [],
            ),
            limites=limites,
            rede_habilitada=bool(corpo.get("rede_habilitada")),
            roteiro_simulado=list(corpo.get("roteiro_simulado") or []),
        )

    def _resolver_segredos(self, slug: str, declaradas: list[dict[str, Any]]) -> dict[str, str]:
        """Traduz as variáveis declaradas em valores, lendo do ambiente do runner.

        O valor de um segredo **nunca** passa pelo banco, pela API nem pela
        interface — o manifesto declara apenas o nome (ver
        `hub_catalog.manifesto.VariavelDeAmbiente`). É aqui, no último momento
        possível, que o valor é resolvido.

        A convenção de nome é longa de propósito:

            SEGREDO_<SLUG_EM_MAIUSCULAS>__<NOME_DA_VARIAVEL>

        O prefixo por automação impede que uma automação leia o segredo de
        outra: cada uma só recebe as variáveis cujo nome casa com o próprio
        slug. Sem isso, uma automação nova no catálogo passaria a receber todas
        as credenciais configuradas no runner.
        """
        import os

        prefixo = "SEGREDO_" + slug.upper().replace("-", "_") + "__"
        resolvidas: dict[str, str] = {}
        faltando: list[str] = []

        for declaracao in declaradas:
            nome = str(declaracao.get("nome") or "")
            if not nome:
                continue
            valor = os.getenv(prefixo + nome)
            if valor is not None:
                resolvidas[nome] = valor
            elif declaracao.get("obrigatorio"):
                faltando.append(nome)

        if faltando:
            # Não é erro aqui: a automação é quem decide o que fazer sem a
            # variável. Mas registrar permite descobrir configuração ausente
            # sem depender de a automação reclamar.
            log.warning("runner.segredos_ausentes", automacao=slug, variaveis=faltando)

        return resolvidas

    # ── Artefatos ────────────────────────────────────────────────────────────

    async def _salvar_artefatos(
        self, execucao_id: str, resultado: ResultadoExecucao, coletor: ColetorDeLogs
    ) -> list[dict[str, Any]]:
        """Guarda os arquivos produzidos e devolve os metadados.

        O caminho inclui o id da execução: `execucoes/<id>/<arquivo>`. Assim um
        artefato nunca sobrescreve o de outra execução, e apagar os artefatos
        de uma execução é apagar um prefixo.
        """
        salvos: list[dict[str, Any]] = []

        for artefato in resultado.artefatos:
            caminho = f"execucoes/{execucao_id}/{artefato.caminho_relativo}"
            try:
                meta = await self._storage.salvar(
                    caminho, artefato.conteudo, tipo_conteudo=artefato.tipo_conteudo
                )
            except Exception as erro:
                # Artefato que não cabe (teto do backend PostgreSQL) ou storage
                # indisponível não invalidam a execução: ela rodou e produziu
                # resultado. O problema vira log visível na própria execução,
                # onde quem a disparou vai ler.
                await coletor.registrar(
                    "ERROR",
                    f"não foi possível guardar o artefato {artefato.caminho_relativo}: {erro}",
                )
                log.warning(
                    "runner.artefato_nao_salvo",
                    execucao_id=execucao_id,
                    caminho=caminho,
                    erro=str(erro)[:200],
                )
                continue

            salvos.append(
                {
                    "nome_arquivo": artefato.caminho_relativo,
                    "caminho_storage": meta.caminho,
                    "tamanho_bytes": meta.tamanho_bytes,
                    "tipo_conteudo": meta.tipo_conteudo,
                    "sha256": meta.sha256,
                }
            )

        return salvos

    # ── Callbacks ────────────────────────────────────────────────────────────

    def _cabecalhos(self) -> dict[str, str]:
        return {"x-orbita-servico": emitir_token_de_servico("runner", self._settings)}

    async def _reportar_inicio(self, execucao_id: str, tentativa: int) -> None:
        try:
            await self._cliente.post(
                f"/_internos/execucoes/{execucao_id}/inicio",
                json={"executor": self._executor.nome, "tentativa": tentativa},
                cabecalhos=self._cabecalhos(),
            )
        except Exception as erro:
            raise FalhaDeInfraestrutura(f"não foi possível registrar o início: {erro}") from erro

    async def _reportar_finalizacao(
        self,
        execucao_id: str,
        resultado: ResultadoExecucao,
        artefatos: list[dict[str, Any]],
    ) -> None:
        corpo = {
            "status": resultado.status.value,
            "codigo_saida": resultado.codigo_saida,
            "resumo": resultado.resumo[:500],
            "erro": resultado.erro,
            "metricas": resultado.metricas,
            "artefatos": artefatos,
            "consumo": {**resultado.consumo, "executor": self._executor.nome},
        }
        try:
            await self._cliente.post(
                f"/_internos/execucoes/{execucao_id}/finalizacao",
                json=corpo,
                cabecalhos=self._cabecalhos(),
            )
        except Exception as erro:
            raise FalhaDeInfraestrutura(
                f"não foi possível registrar a finalização: {erro}"
            ) from erro

    # ── Operação ─────────────────────────────────────────────────────────────

    async def cancelar(self, execucao_id: str) -> bool:
        """Pede ao executor que interrompa uma execução em andamento."""
        return await self._executor.cancelar(execucao_id)

    def situacao(self) -> dict[str, Any]:
        return {
            "executor": self._executor.nome,
            "max_concorrentes": self._max_concorrentes,
            "em_execucao": len(self._em_execucao),
            "vagas_livres": self._max_concorrentes - len(self._em_execucao),
            "processadas": self.processadas,
            "status_terminais": [s.value for s in StatusExecucao if s.terminal],
        }
