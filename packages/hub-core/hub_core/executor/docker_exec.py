"""Executor Docker: roda cada execução em um container próprio.

É o executor de produção, e o único que oferece isolamento de verdade: cgroups
para CPU e memória, namespace de rede, sistema de arquivos somente leitura e
usuário sem privilégio.

## A parte difícil: log ao vivo com uma biblioteca síncrona

O `docker` SDK é síncrono. `container.logs(stream=True, follow=True)` devolve um
gerador **bloqueante**: cada `next()` espera a próxima linha chegar. Iterar isso
dentro de uma corrotina congela o event loop, e, com um container que roda por
dez minutos, congela o processo inteiro por dez minutos.

A solução aqui é a ponte padrão entre os dois mundos: uma thread consome o
gerador bloqueante e empurra cada linha em uma `asyncio.Queue` usando
`loop.call_soon_threadsafe`; a corrotina lê dessa fila. O event loop nunca
bloqueia, e o log chega à interface conforme é produzido.

`asyncio.to_thread` resolveria se fosse uma chamada só, mas não serve para um
gerador que produz ao longo do tempo, ele devolve o resultado no fim, e o fim
é justamente o que não se quer esperar.

## Como a plataforma conversa com o container

Os parâmetros entram e os artefatos saem pela API de archive do daemon, por
dois volumes criados por execução, e não pela camada do container nem por um
`tmpfs`. Os dois caminhos ingênuos falham calado, cada um por um motivo
diferente. Ver ADR 0014 e os comentários de `OPCOES_TMPFS_TRABALHO` e
`DIR_DE_CONTROLE`.

## Postura de segurança do container

Toda execução roda com: rede desligada por padrão, raiz somente leitura,
`no-new-privileges`, todas as *capabilities* descartadas, limite de PIDs, e uma
área de trabalho com teto de tamanho. O que a automação precisa além disso,
declara no manifesto, e a declaração fica visível na ficha dela.
"""

from __future__ import annotations

import asyncio
import io
import json
import tarfile
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from hub_core.errors import ImagemIndisponivel
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

# O diretorio de trabalho e um volume do driver `local` com backing de tmpfs,
# e nao um `tmpfs` do HostConfig. A diferenca decide se a plataforma consegue
# ou nao colher os artefatos.
#
# Um tmpfs declarado em `HostConfig.Tmpfs` e criado pelo runc DENTRO do
# namespace de montagem do container. A API de archive do daemon, que e como
# `get_archive` le arquivos, resolve caminhos no rootfs do graphdriver mais os
# volumes registrados, do lado do host. Sao dois universos: o arquivo existe
# para a automacao e nao existe para o daemon, que responde
# `404 Could not find the file /trabalho/... in container`, com o container
# rodando e o arquivo gravado. Nao e questao de momento.
#
# Volume do driver `local` com `type=tmpfs` resolve sem abrir mao de nada: o
# tmpfs e montado em `/var/lib/docker/volumes/<nome>/_data`, no host, e o
# container recebe um bind daquele caminho. Um tmpfs so, visivel dos dois
# lados. O teto de tamanho continua valendo (e opcao de montagem do tmpfs, nao
# do Docker), o conteudo continua em RAM e continua sumindo no fim, e agora o
# `get_archive` enxerga.
OPCOES_TMPFS_TRABALHO = "size={disco_mb}m,mode=1777"

# Os parametros chegam por um volume proprio, montado em `/orbita`, e cada
# parte dessa frase e consequencia de uma restricao do Docker.
#
# Nao da para gravar na raiz: `read_only=True` marca o rootfs como somente
# leitura ja na *criacao* do container, nao no start, e o daemon recusa a
# extracao com `400 container rootfs is marked read-only`. Vale para qualquer
# caminho da camada do container, parado ou rodando.
#
# E nao serve gravar no diretorio de trabalho, embora hoje ele tambem seja um
# volume e aceitasse a escrita: `/trabalho` e montado 1777 porque a automacao
# precisa escrever ali, e um arquivo em diretorio 1777 pode ser renomeado ou
# substituido pelo dono do novo nome. Em um volume separado, montado com o
# diretorio raiz root:root, a automacao le os proprios parametros e nao tem
# como reescreve-los no meio da execucao. `/trabalho` tambem e enumerado como
# area de artefatos, e parametro de entrada nao e artefato de saida.
#
# Volume funciona porque o daemon abre uma excecao explicita: antes de checar
# o rootfs somente leitura, ele verifica se o destino cai dentro de um volume
# montado para escrita (`checkIfPathIsInAVolume`, em `daemon/archive.go`) e,
# nesse caso, permite. Os volumes sao montados tambem para o container
# *parado*, e e isso que torna possivel entregar os parametros ANTES do start,
# sem corrida contra a automacao lendo o arquivo.
#
# E volume, nao bind mount, por um segundo motivo: o runner e ele mesmo um
# container falando com o socket do host. Um caminho de bind seria resolvido
# no host, nao no filesystem do runner. Volume e gerenciado pelo daemon e nao
# precisa de traducao de caminho nenhuma.
DIR_DE_CONTROLE = "/orbita"
ARQUIVO_DE_PARAMETROS = f"{DIR_DE_CONTROLE}/parametros.json"


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
            except Exception as erro_local:
                # Levanta, em vez de devolver FALHA: a automação não teve a sua
                # chance, então isto é falha de infraestrutura, não resultado de
                # automação. O worker dá nack e a mensagem volta para a fila
                # (ver `hub_runner.worker`), e o executor de reserva, quando
                # configurado, intercepta antes disso.
                raise ImagemIndisponivel(
                    f"não foi possível obter a imagem {pedido.imagem}",
                    imagem=pedido.imagem,
                    detalhe=str(erro)[:300],
                ) from erro_local

        metricas: dict[str, float] = {}
        # Colhidos DURANTE a execucao, nao depois. Ver `_colher_agora`.
        artefatos: list[ArtefatoProduzido] = []
        resumo = ""

        # Nome deterministico, para que `docker ps` seja legivel e para que
        # cada execucao tenha no maximo um container. O preco e que uma sobra
        # de tentativa anterior bloqueia a proxima com 409, e isso acontece de
        # verdade: se o runner morre entre o `create` e o `remove`, o container
        # fica em `Created` para sempre e TODA retentativa da mesma execucao
        # falha antes de comecar, ate a mensagem morrer na DLQ. Limpar a sobra
        # aqui e o que torna a retentativa util.
        nome = f"orbita-{pedido.automacao_slug[:30]}-{pedido.execucao_id[:8]}"
        vol_controle = f"orbita-ctl-{pedido.execucao_id[:8]}"
        vol_trabalho = f"orbita-wk-{pedido.execucao_id[:8]}"
        container: Any = None

        # Tudo daqui para baixo dentro do mesmo `try`: antes, a entrega dos
        # parametros ficava entre o `create` e o `try`, e uma falha ali deixava
        # o container criado e nunca removido, que e exatamente a sobra
        # descrita acima. Qualquer passo novo entre criar e iniciar entra aqui.
        try:
            await asyncio.to_thread(self._limpar_sobras, nome, (vol_controle, vol_trabalho))
            await asyncio.to_thread(self._criar_volume, vol_controle, pedido)
            await asyncio.to_thread(
                self._criar_volume,
                vol_trabalho,
                pedido,
                OPCOES_TMPFS_TRABALHO.format(disco_mb=pedido.limites.disco_mb),
            )

            container = await asyncio.to_thread(
                self._criar_container, pedido, nome, vol_controle, vol_trabalho
            )
            self._containers[pedido.execucao_id] = container

            await asyncio.to_thread(self._enviar_parametros, container, pedido)

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
                            # Colhe na hora, enquanto o container ainda roda.
                            #
                            # O volume de trabalho sobrevive ao container, e
                            # colher depois do `wait` tambem funcionaria. O
                            # motivo de colher aqui e outro: execucao que bate
                            # no timeout, ou que falha no meio, entrega os
                            # artefatos que ja produziu em vez de nenhum. Em
                            # automacao longa, e a diferenca entre ter o que
                            # examinar e nao ter.
                            #
                            # E seguro porque o SDK grava o arquivo ANTES de
                            # anunciar: quando a linha chega, o conteudo esta
                            # completo.
                            colhido = await self._colher_agora(
                                container, str(dados.get("caminho", ""))
                            )
                            if colhido is not None:
                                artefatos.append(colhido)
                            else:
                                await ao_registrar_log(
                                    "WARNING",
                                    f"artefato declarado mas nao encontrado: "
                                    f"{dados.get('caminho')}",
                                )
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
                        f"OOMKilled, o limite é {pedido.limites.memoria_mb} MB. "
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
            if container is not None:
                await asyncio.to_thread(self._remover, container)
            # Depois do container, nunca antes: volume em uso nao e removivel.
            for volume in (vol_controle, vol_trabalho):
                await asyncio.to_thread(self._remover_volume, volume)

    def _criar_volume(
        self, nome: str, pedido: PedidoDeExecucao, opcoes_tmpfs: str | None = None
    ) -> None:
        """Volume efemero da execucao, removido no `finally` de `executar`.

        Com `opcoes_tmpfs`, o volume e respaldado por tmpfs em vez de disco.
        Ver o comentario de `OPCOES_TMPFS_TRABALHO`.
        """
        extra: dict[str, Any] = {}
        if opcoes_tmpfs is not None:
            extra["driver_opts"] = {"type": "tmpfs", "device": "tmpfs", "o": opcoes_tmpfs}

        self._cliente.volumes.create(
            name=nome,
            driver="local",
            labels={
                "orbita.execucao": pedido.execucao_id,
                "orbita.automacao": pedido.automacao_slug,
                "orbita.gerenciado": "true",
            },
            **extra,
        )

    def _remover_volume(self, nome: str) -> None:
        try:
            self._cliente.volumes.get(nome).remove(force=True)
        except self._docker_mod.errors.NotFound:
            pass
        except Exception:
            log.warning("executor.volume_nao_removido", volume=nome, exc_info=True)

    def _limpar_sobras(self, nome: str, volumes: tuple[str, ...]) -> None:
        """Apaga container e volumes de uma tentativa anterior desta execucao.

        Registra em WARNING porque nao e normal: significa que a tentativa
        anterior com este id nao limpou atras de si, e saber disso vale mais do
        que o silencio de uma limpeza automatica.
        """
        try:
            anterior = self._cliente.containers.get(nome)
        except self._docker_mod.errors.NotFound:
            pass
        except Exception:
            log.warning("executor.consulta_de_sobra_falhou", nome=nome, exc_info=True)
        else:
            log.warning(
                "executor.container_orfao_removido",
                nome=nome,
                container=anterior.short_id,
                status=anterior.status,
            )
            self._remover(anterior)

        # Depois do container: o volume em uso por ele nao seria removivel.
        for volume in volumes:
            try:
                self._cliente.volumes.get(volume)
            except Exception:
                continue
            log.warning("executor.volume_orfao_removido", volume=volume)
            self._remover_volume(volume)

    def _enviar_parametros(self, container: Any, pedido: PedidoDeExecucao) -> None:
        """Grava `parametros.json` dentro do container, antes de inicia-lo.

        Por arquivo, e nao por variavel de ambiente, pelo mesmo motivo do
        executor de subprocesso: parametro de automacao pode conter dado
        sensivel, e variavel de ambiente de container aparece inteira em
        `docker inspect`, que qualquer um com acesso ao socket consegue rodar.
        O arquivo so e legivel de dentro do container.
        """
        corpo = json.dumps(pedido.parametros, ensure_ascii=False, indent=2).encode("utf-8")

        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as arquivo_tar:
            info = tarfile.TarInfo(name="parametros.json")
            info.size = len(corpo)
            # 0444 e dono root:root (o padrao do TarInfo). A automacao roda
            # como 1000:1000, entao le e nao escreve: nem o processo dela pode
            # reescrever os proprios parametros no meio da execucao.
            info.mode = 0o444
            arquivo_tar.addfile(info, io.BytesIO(corpo))
        buffer.seek(0)

        # Destino e o ponto de montagem do volume, e nao `/`. Ver o comentario
        # de DIR_DE_CONTROLE: em `/` o daemon recusa com 400, porque o rootfs
        # esta marcado somente leitura desde a criacao do container.
        container.put_archive(DIR_DE_CONTROLE, buffer.getvalue())

    def _criar_container(
        self, pedido: PedidoDeExecucao, nome: str, vol_controle: str, vol_trabalho: str
    ) -> Any:
        """Cria o container com a postura de segurança restritiva."""
        return self._cliente.containers.create(
            image=pedido.imagem,
            command=pedido.entrypoint or None,
            name=nome,
            environment={
                **pedido.variaveis_ambiente,
                "ORBITA_EXECUCAO_ID": pedido.execucao_id,
                "ORBITA_AUTOMACAO": pedido.automacao_slug,
                "ORBITA_DIR_TRABALHO": DIR_TRABALHO_CONTAINER,
                # Caminho do arquivo de parametros, gravado por
                # `_enviar_parametros` entre a criacao e o start. Ate esta
                # correcao o executor Docker simplesmente nao entregava os
                # parametros: a automacao caia nos valores padrao e nada no
                # log indicava o problema, so o resultado saia da competencia
                # errada.
                "ORBITA_PARAMETROS": ARQUIVO_DE_PARAMETROS,
                "PYTHONUNBUFFERED": "1",
            },
            # Sem `working_dir`: vale o WORKDIR da própria imagem, que é onde
            # o código da automação está.
            #
            # Antes isto apontava para `DIR_TRABALHO_CONTAINER`, e o efeito era
            # que `entrypoint: [python, -I, main.py]` rodava a partir do
            # diretório de artefatos, onde não há código:
            #
            #     python: can't open file '/trabalho/main.py'
            #
            # Os dois diretórios têm papéis diferentes e não devem ser o
            # mesmo: o processo roda onde está o código, e escreve os
            # artefatos no caminho que recebe em `ORBITA_DIR_TRABALHO`. É
            # assim que o executor de subprocesso sempre funcionou (`cwd` no
            # diretório da automação, artefatos pelo ambiente), e os dois
            # precisam concordar — senão a mesma automação, com o mesmo
            # manifesto, roda em um executor e falha no outro.
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
            # Os dois montados `rw`, e nos dois casos por exigencia do daemon,
            # nao por necessidade da automacao: a API de archive recusa tanto
            # extrair quanto ler em volume montado somente leitura, com
            # `ErrVolumeReadonly`, antes de olhar o conteudo. No volume de
            # controle, quem protege o arquivo e o modo 0444 root:root dele.
            volumes={
                vol_controle: {"bind": DIR_DE_CONTROLE, "mode": "rw"},
                vol_trabalho: {"bind": DIR_TRABALHO_CONTAINER, "mode": "rw"},
            },
            # `/tmp` segue tmpfs do HostConfig, e pode: nada aqui precisa ler
            # de volta o que a automacao escreve ali.
            tmpfs={"/tmp": "rw,size=64m,mode=1777"},
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

    async def _colher_agora(self, container: Any, caminho: str) -> ArtefatoProduzido | None:
        """Extrai UM arquivo de dentro do container, com ele ainda rodando.

        `get_archive` devolve um stream tar. E o unico jeito de ler arquivo de
        um container sem montar volume do host, e nao montar volume do host e
        o que mantem o host fora do alcance da automacao.
        """
        if not caminho or ".." in caminho or caminho.startswith("/"):
            log.warning("artefato.caminho_recusado", caminho=caminho)
            return None

        destino = f"{DIR_TRABALHO_CONTAINER}/{caminho}"
        try:
            stream, _ = await asyncio.to_thread(container.get_archive, destino)
            bruto = b"".join(stream)
            with tarfile.open(fileobj=io.BytesIO(bruto)) as arquivo_tar:
                membro = next((m for m in arquivo_tar.getmembers() if m.isfile()), None)
                if membro is None:
                    return None
                extraido = arquivo_tar.extractfile(membro)
                if extraido is None:
                    return None
                return ArtefatoProduzido(
                    caminho_relativo=caminho,
                    conteudo=extraido.read(),
                    tipo_conteudo=tipo_conteudo_por_extensao(caminho),
                )
        except Exception:
            log.warning("artefato.extracao_falhou", caminho=caminho, exc_info=True)
            return None

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

        O filtro por label é o que impede a interface de listar, e oferecer
        um botão de "parar" para, containers que não são da plataforma. Em uma
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
