"""Executor Docker: roda cada execução em um container próprio.

É o executor de produção, e o único que oferece isolamento de verdade: cgroups
para CPU e memória, namespace de rede, sistema de arquivos somente leitura e
usuário sem privilégio.

## A parte difícil: log ao vivo com uma biblioteca síncrona

O `docker` SDK é síncrono. `container.logs(stream=True, follow=True)` devolve um
gerador **bloqueante**: cada `next()` espera a próxima linha chegar. Iterar isso
dentro de uma corrotina congela o event loop — e, com um container que roda por
dez minutos, congela o processo inteiro por dez minutos.

A solução aqui é a ponte padrão entre os dois mundos: uma thread consome o
gerador bloqueante e empurra cada linha em uma `asyncio.Queue` usando
`loop.call_soon_threadsafe`; a corrotina lê dessa fila. O event loop nunca
bloqueia, e o log chega à interface conforme é produzido.

`asyncio.to_thread` resolveria se fosse uma chamada só, mas não serve para um
gerador que produz ao longo do tempo — ele devolve o resultado no fim, e o fim
é justamente o que não se quer esperar.

## Postura de segurança do container

Toda execução roda com: rede desligada por padrão, raiz somente leitura,
`no-new-privileges`, todas as *capabilities* descartadas, limite de PIDs, e um
`tmpfs` com teto para a área de trabalho. O que a automação precisa além disso,
declara no manifesto — e a declaração fica visível na ficha dela.
"""

from __future__ import annotations

import asyncio
import io
import tarfile
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from hub_core.executor.base import (
    AoRegistrarLog,
    ArtefatoProduzido,
    Executor,
    PedidoDeExecucao,
    ResultadoExecucao,
    StatusExecucao,
    interpretar_linha,
)
from hub_core.executor.subprocesso import tipo_conteudo_por_extensao
from hub_core.logging import obter_logger

log = obter_logger(__name__)

DIR_TRABALHO_CONTAINER = "/trabalho"


class ExecutorDocker(Executor):
    nome = "docker"

    def __init__(self, *, max_threads: int = 8) -> None:
        try:
            import docker
        except ImportError as erro:  # pragma: no cover
            raise RuntimeError(
                "EXECUTOR_BACKEND=docker exige o extra 'docker': pip install 'orbita-hub[docker]'"
            ) from erro

        self._docker_mod = docker
        self._cliente: Any = None
        self._threads = ThreadPoolExecutor(max_workers=max_threads, thread_name_prefix="docker")
        self._containers: dict[str, Any] = {}

    async def iniciar(self) -> None:
        def _conectar() -> Any:
            cliente = self._docker_mod.from_env()
            cliente.ping()
            return cliente

        self._cliente = await asyncio.to_thread(_conectar)
        versao = await asyncio.to_thread(lambda: self._cliente.version().get("Version"))
        log.info("executor.docker_conectado", versao=versao)

    async def encerrar(self) -> None:
        self._threads.shutdown(wait=False, cancel_futures=True)
        if self._cliente is not None:
            await asyncio.to_thread(self._cliente.close)
        self._cliente = None

    async def saudavel(self) -> tuple[bool, str]:
        if self._cliente is None:
            return False, "cliente Docker não iniciado"
        try:
            await asyncio.to_thread(self._cliente.ping)
        except Exception as erro:
            return False, f"Docker Engine inacessível: {erro}"
        return True, "Docker Engine acessível"

    async def executar(
        self,
        pedido: PedidoDeExecucao,
        ao_registrar_log: AoRegistrarLog,
    ) -> ResultadoExecucao:
        if self._cliente is None:
            await self.iniciar()

        await ao_registrar_log("INFO", f"[docker] obtendo imagem {pedido.imagem}")
        try:
            await asyncio.to_thread(self._cliente.images.pull, pedido.imagem)
        except Exception as erro:
            # Imagem pode já existir localmente (build local, registry privado
            # sem credencial no ambiente). Só falha se também não estiver aqui.
            try:
                await asyncio.to_thread(self._cliente.images.get, pedido.imagem)
                await ao_registrar_log("DEBUG", "imagem já presente localmente")
            except Exception:
                return ResultadoExecucao(
                    status=StatusExecucao.FALHA,
                    resumo="Imagem indisponível",
                    erro=f"não foi possível obter {pedido.imagem}: {erro}",
                )

        container = await asyncio.to_thread(self._criar_container, pedido)
        self._containers[pedido.execucao_id] = container

        metricas: dict[str, float] = {}
        artefatos_declarados: list[str] = []
        resumo = ""

        try:
            await asyncio.to_thread(container.start)
            await ao_registrar_log("INFO", f"container {container.short_id} iniciado")

            fila_linhas = await self._bombear_logs(container)

            async def consumir() -> None:
                nonlocal resumo
                while True:
                    linha = await fila_linhas.get()
                    if linha is None:
                        return
                    comando = interpretar_linha(linha)
                    if comando is None:
                        await ao_registrar_log("INFO", linha)
                        continue
                    dados = comando.dados
                    match comando.tipo:
                        case "log":
                            await ao_registrar_log(
                                str(dados.get("nivel", "INFO")),
                                str(dados.get("mensagem", "")),
                            )
                        case "metrica":
                            try:
                                metricas[str(dados["nome"])] = float(dados["valor"])
                            except (KeyError, TypeError, ValueError):
                                await ao_registrar_log("WARNING", f"métrica inválida: {linha}")
                        case "artefato":
                            artefatos_declarados.append(str(dados.get("caminho", "")))
                        case "resumo":
                            resumo = str(dados.get("mensagem", ""))

            try:
                await asyncio.wait_for(consumir(), timeout=pedido.limites.timeout_segundos)
                saida = await asyncio.to_thread(container.wait)
            except TimeoutError:
                await asyncio.to_thread(container.kill)
                await ao_registrar_log(
                    "ERROR", f"tempo máximo de {pedido.limites.timeout_segundos}s excedido"
                )
                return ResultadoExecucao(
                    status=StatusExecucao.TIMEOUT,
                    resumo="Tempo esgotado",
                    erro=f"excedeu {pedido.limites.timeout_segundos}s",
                    metricas=metricas,
                )

            codigo = int(saida.get("StatusCode", 1))
            artefatos = await self._colher_artefatos(container, artefatos_declarados)
            consumo = await self._consumo(container)

            # 137 = 128 + SIGKILL(9). O Docker usa esse código quando o
            # OOM killer derruba o container, e distinguir isso de uma falha
            # de lógica da automação é a diferença entre "aumente a memória" e
            # "o código tem bug".
            if codigo == 137:
                return ResultadoExecucao(
                    status=StatusExecucao.FALHA,
                    codigo_saida=codigo,
                    resumo="Container encerrado por falta de memória",
                    erro=(
                        f"OOMKilled — o limite é {pedido.limites.memoria_mb} MB. "
                        "Aumente `resources.memory` no manifesto da automação."
                    ),
                    metricas=metricas,
                    artefatos=artefatos,
                    consumo=consumo,
                )

            if codigo != 0:
                return ResultadoExecucao(
                    status=StatusExecucao.FALHA,
                    codigo_saida=codigo,
                    resumo=resumo or f"Container terminou com código {codigo}",
                    erro=f"código de saída {codigo}",
                    metricas=metricas,
                    artefatos=artefatos,
                    consumo=consumo,
                )

            return ResultadoExecucao(
                status=StatusExecucao.SUCESSO,
                codigo_saida=0,
                resumo=resumo or "Execução concluída",
                metricas=metricas,
                artefatos=artefatos,
                consumo=consumo,
            )
        finally:
            self._containers.pop(pedido.execucao_id, None)
            await asyncio.to_thread(self._remover, container)

    def _criar_container(self, pedido: PedidoDeExecucao) -> Any:
        """Cria o container com a postura de segurança restritiva."""
        return self._cliente.containers.create(
            image=pedido.imagem,
            command=pedido.entrypoint or None,
            name=f"orbita-{pedido.automacao_slug[:30]}-{pedido.execucao_id[:8]}",
            environment={
                **pedido.variaveis_ambiente,
                "ORBITA_EXECUCAO_ID": pedido.execucao_id,
                "ORBITA_AUTOMACAO": pedido.automacao_slug,
                "ORBITA_DIR_TRABALHO": DIR_TRABALHO_CONTAINER,
                "PYTHONUNBUFFERED": "1",
            },
            working_dir=DIR_TRABALHO_CONTAINER,
            # `none` desliga a interface de rede do container por completo.
            network_mode="bridge" if pedido.rede_habilitada else "none",
            # Converte a fração de CPU no par period/quota que os cgroups usam.
            cpu_period=100_000,
            cpu_quota=int(pedido.limites.cpus * 100_000),
            mem_limit=f"{pedido.limites.memoria_mb}m",
            # Sem isto, o container pode usar swap do host e o limite de
            # memória deixa de ser um limite.
            memswap_limit=f"{pedido.limites.memoria_mb}m",
            pids_limit=256,
            read_only=True,
            tmpfs={
                DIR_TRABALHO_CONTAINER: f"rw,size={pedido.limites.disco_mb}m,mode=1777",
                "/tmp": "rw,size=64m,mode=1777",
            },
            security_opt=["no-new-privileges:true"],
            cap_drop=["ALL"],
            user="1000:1000",
            labels={
                "orbita.execucao": pedido.execucao_id,
                "orbita.automacao": pedido.automacao_slug,
                "orbita.gerenciado": "true",
            },
            detach=True,
        )

    async def _bombear_logs(self, container: Any) -> asyncio.Queue[str | None]:
        """Liga o gerador bloqueante de logs a uma fila assíncrona.

        A thread faz o trabalho bloqueante; `call_soon_threadsafe` é o único
        jeito seguro de tocar no event loop de fora dele. `None` na fila
        sinaliza fim do stream.
        """
        fila: asyncio.Queue[str | None] = asyncio.Queue(maxsize=1000)
        loop = asyncio.get_running_loop()

        def bombear() -> None:
            try:
                for pedaco in container.logs(stream=True, follow=True, stdout=True, stderr=True):
                    for linha in pedaco.decode("utf-8", errors="replace").splitlines():
                        if linha.strip():
                            loop.call_soon_threadsafe(fila.put_nowait, linha)
            except Exception:
                log.warning("executor.docker_log_interrompido", exc_info=True)
            finally:
                loop.call_soon_threadsafe(fila.put_nowait, None)

        loop.run_in_executor(self._threads, bombear)
        return fila

    async def _colher_artefatos(
        self, container: Any, declarados: list[str]
    ) -> list[ArtefatoProduzido]:
        """Extrai os arquivos declarados de dentro do container.

        `get_archive` devolve um stream tar. É o único jeito de ler arquivo de
        um container sem montar volume do host — e não montar volume é o que
        mantém o host fora do alcance da automação.
        """
        colhidos: list[ArtefatoProduzido] = []
        for caminho in declarados:
            if not caminho or ".." in caminho:
                continue
            destino = f"{DIR_TRABALHO_CONTAINER}/{caminho.lstrip('/')}"
            try:
                stream, _ = await asyncio.to_thread(container.get_archive, destino)
                bruto = b"".join(stream)
                with tarfile.open(fileobj=io.BytesIO(bruto)) as arquivo_tar:
                    membro = next((m for m in arquivo_tar.getmembers() if m.isfile()), None)
                    if membro is None:
                        continue
                    extraido = arquivo_tar.extractfile(membro)
                    if extraido is None:
                        continue
                    colhidos.append(
                        ArtefatoProduzido(
                            caminho_relativo=caminho,
                            conteudo=extraido.read(),
                            tipo_conteudo=tipo_conteudo_por_extensao(caminho),
                        )
                    )
            except Exception:
                log.warning("artefato.extracao_falhou", caminho=caminho, exc_info=True)
        return colhidos

    async def _consumo(self, container: Any) -> dict[str, Any]:
        try:
            estatisticas = await asyncio.to_thread(container.stats, stream=False)
        except Exception:
            return {"simulado": False, "executor": self.nome}

        memoria = estatisticas.get("memory_stats", {})
        cpu = estatisticas.get("cpu_stats", {}).get("cpu_usage", {})
        return {
            "simulado": False,
            "executor": self.nome,
            "memoria_pico_mb": round(memoria.get("max_usage", 0) / (1024 * 1024), 1),
            "memoria_limite_mb": round(memoria.get("limit", 0) / (1024 * 1024), 1),
            "cpu_segundos": round(cpu.get("total_usage", 0) / 1e9, 2),
        }

    def _remover(self, container: Any) -> None:
        try:
            container.remove(force=True)
        except Exception:
            log.warning("executor.remocao_falhou", exc_info=True)

    async def cancelar(self, execucao_id: str) -> bool:
        container = self._containers.get(execucao_id)
        if container is None:
            return False
        await asyncio.to_thread(container.kill)
        return True

    # ── Módulo de containers: administração do que está rodando ──────────────

    async def listar_containers(self) -> list[dict[str, Any]]:
        """Containers gerenciados pela plataforma, para a área de Runners.

        O filtro por label é o que impede a interface de listar — e oferecer
        um botão de "parar" para — containers que não são da plataforma. Em uma
        máquina de desenvolvimento, isso evita derrubar o próprio PostgreSQL.
        """
        if self._cliente is None:
            return []

        def _listar() -> list[dict[str, Any]]:
            containers = self._cliente.containers.list(
                all=True, filters={"label": "orbita.gerenciado=true"}
            )
            return [
                {
                    "id": c.short_id,
                    "nome": c.name,
                    "imagem": (c.image.tags[0] if c.image.tags else c.image.short_id),
                    "status": c.status,
                    "execucao_id": c.labels.get("orbita.execucao"),
                    "automacao": c.labels.get("orbita.automacao"),
                    "criado_em": c.attrs.get("Created"),
                }
                for c in containers
            ]

        return await asyncio.to_thread(_listar)
