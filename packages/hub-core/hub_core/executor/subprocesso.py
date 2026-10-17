"""Executor de subprocesso: roda a automação como processo local.

É o meio-termo entre o simulado e o Docker. Executa **código de verdade** — as
automações versionadas em `automations/` — sem exigir Docker Engine. É o que
permite rodar o pipeline completo na máquina de quem desenvolve, e o que roda
as automações embutidas na demonstração pública.

## O que ele isola, e o que não isola

Isola:

* **sistema de arquivos de trabalho** — cada execução recebe um diretório
  temporário próprio, e é dali que os artefatos são colhidos;
* **ambiente** — o processo filho não herda o `os.environ` do servidor. Recebe
  uma lista explícita, o que impede a automação de ler `DATABASE_URL`,
  `JWT_SECRET` ou credencial de nuvem do processo pai. Esse é o ponto mais
  importante deste módulo;
* **tempo** — timeout com encerramento em duas etapas.

Não isola:

* **memória e CPU.** Em Linux daria com `resource.setrlimit` via `preexec_fn`;
  no Windows não há equivalente portátil, e um limite que vale em um sistema
  operacional e não no outro é pior do que nenhum, porque gera confiança
  indevida. Quem precisa de teto real usa `EXECUTOR_BACKEND=docker`, onde são
  cgroups de verdade;
* **rede.** O processo filho alcança a rede do host.

Por isso este executor só deve rodar automação do próprio repositório, nunca
imagem informada por usuário. A plataforma reforça isso: automação com
`imagem` externa é rejeitada quando o backend é `subprocess`.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from hub_core.executor.base import (
    AoRegistrarLog,
    ArtefatoProduzido,
    Executor,
    PedidoDeExecucao,
    ResultadoExecucao,
    StatusExecucao,
    interpretar_linha,
)
from hub_core.logging import obter_logger

log = obter_logger(__name__)

# Variáveis que o processo filho recebe do ambiente do servidor. Tudo que não
# estiver nesta lista fica de fora — inclusive segredo, credencial de nuvem e
# URL de banco. A lista é curta por desenho: o que falta aqui a automação
# declara no manifesto.
_AMBIENTE_PERMITIDO = ("PATH", "LANG", "LC_ALL", "TZ", "SYSTEMROOT", "TEMP", "TMP")

_TIPOS_POR_EXTENSAO = {
    ".csv": "text/csv; charset=utf-8",
    ".json": "application/json",
    ".txt": "text/plain; charset=utf-8",
    ".pdf": "application/pdf",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".zip": "application/zip",
    ".html": "text/html; charset=utf-8",
}


class ExecutorSubprocesso(Executor):
    nome = "subprocesso"

    def __init__(self, *, raiz_automacoes: Path | str | None = None) -> None:
        self._raiz = Path(raiz_automacoes or _descobrir_raiz_automacoes())
        self._processos: dict[str, asyncio.subprocess.Process] = {}

    async def saudavel(self) -> tuple[bool, str]:
        if not self._raiz.is_dir():
            return False, f"diretório de automações não encontrado: {self._raiz}"
        return True, f"automações em {self._raiz}"

    async def executar(
        self,
        pedido: PedidoDeExecucao,
        ao_registrar_log: AoRegistrarLog,
    ) -> ResultadoExecucao:
        diretorio_automacao = self._raiz / pedido.automacao_slug
        if not diretorio_automacao.is_dir():
            return ResultadoExecucao(
                status=StatusExecucao.FALHA,
                resumo="Automação não encontrada neste executor",
                erro=(
                    f"não existe {diretorio_automacao}. O executor de subprocesso só roda "
                    "automações versionadas no repositório; para imagem externa use "
                    "EXECUTOR_BACKEND=docker."
                ),
            )

        comando = pedido.entrypoint or [sys.executable, "-I", "main.py"]
        trabalho = Path(tempfile.mkdtemp(prefix=f"orbita-{pedido.execucao_id[:8]}-"))

        try:
            return await self._rodar(
                pedido, comando, diretorio_automacao, trabalho, ao_registrar_log
            )
        finally:
            shutil.rmtree(trabalho, ignore_errors=True)

    async def _rodar(
        self,
        pedido: PedidoDeExecucao,
        comando: list[str],
        diretorio_automacao: Path,
        trabalho: Path,
        ao_registrar_log: AoRegistrarLog,
    ) -> ResultadoExecucao:
        # Os parâmetros vão por arquivo, não por linha de comando: argumento de
        # processo é visível em `ps` para qualquer usuário da máquina, e
        # parâmetro de automação pode conter dado sensível.
        (trabalho / "parametros.json").write_text(
            json.dumps(pedido.parametros, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        ambiente = {
            chave: os.environ[chave] for chave in _AMBIENTE_PERMITIDO if chave in os.environ
        }
        ambiente.update(pedido.variaveis_ambiente)
        ambiente.update(
            {
                "ORBITA_EXECUCAO_ID": pedido.execucao_id,
                "ORBITA_AUTOMACAO": pedido.automacao_slug,
                "ORBITA_DIR_TRABALHO": str(trabalho),
                "ORBITA_PARAMETROS": str(trabalho / "parametros.json"),
                # `-I` no interpretador já isola, mas automações que invocam
                # subprocessos próprios herdam isto:
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONUNBUFFERED": "1",
            }
        )

        await ao_registrar_log(
            "INFO", f"[subprocesso] {' '.join(comando)} (cwd={diretorio_automacao.name})"
        )

        processo = await asyncio.create_subprocess_exec(
            *comando,
            cwd=str(diretorio_automacao),
            env=ambiente,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        self._processos[pedido.execucao_id] = processo

        metricas: dict[str, float] = {}
        artefatos_declarados: list[str] = []
        resumo = ""

        async def consumir_saida() -> None:
            nonlocal resumo
            assert processo.stdout is not None
            async for linha_bruta in processo.stdout:
                linha = linha_bruta.decode("utf-8", errors="replace").rstrip("\r\n")
                if not linha:
                    continue
                comando_protocolo = interpretar_linha(linha)
                if comando_protocolo is None:
                    await ao_registrar_log("INFO", linha)
                    continue

                dados = comando_protocolo.dados
                match comando_protocolo.tipo:
                    case "log":
                        await ao_registrar_log(
                            str(dados.get("nivel", "INFO")), str(dados.get("mensagem", ""))
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
                    case _:
                        await ao_registrar_log("DEBUG", f"comando desconhecido: {linha}")

        try:
            await asyncio.wait_for(
                asyncio.gather(consumir_saida(), processo.wait()),
                timeout=pedido.limites.timeout_segundos,
            )
        except TimeoutError:
            await self._encerrar_processo(processo)
            await ao_registrar_log(
                "ERROR", f"tempo máximo de {pedido.limites.timeout_segundos}s excedido"
            )
            return ResultadoExecucao(
                status=StatusExecucao.TIMEOUT,
                resumo="Tempo esgotado",
                erro=f"excedeu {pedido.limites.timeout_segundos}s",
                metricas=metricas,
            )
        finally:
            self._processos.pop(pedido.execucao_id, None)

        codigo = processo.returncode or 0
        artefatos = await asyncio.to_thread(_colher_artefatos, trabalho, artefatos_declarados)

        if codigo != 0:
            return ResultadoExecucao(
                status=StatusExecucao.FALHA,
                codigo_saida=codigo,
                resumo=resumo or f"Processo terminou com código {codigo}",
                erro=f"código de saída {codigo}",
                metricas=metricas,
                artefatos=artefatos,
            )

        return ResultadoExecucao(
            status=StatusExecucao.SUCESSO,
            codigo_saida=0,
            resumo=resumo or "Execução concluída",
            metricas=metricas,
            artefatos=artefatos,
            consumo={"simulado": False, "executor": self.nome},
        )

    async def cancelar(self, execucao_id: str) -> bool:
        processo = self._processos.get(execucao_id)
        if processo is None:
            return False
        await self._encerrar_processo(processo)
        return True

    @staticmethod
    async def _encerrar_processo(processo: asyncio.subprocess.Process) -> None:
        """Encerra em duas etapas: pede, depois força.

        `terminate` dá à automação a chance de fechar arquivo e conexão. Se ela
        ignorar — ou estiver travada em I/O — cinco segundos depois vem o
        `kill`. Sem a segunda etapa, um processo preso deixa a execução
        pendurada para sempre.
        """
        if processo.returncode is not None:
            return
        processo.terminate()
        try:
            await asyncio.wait_for(processo.wait(), timeout=5)
        except TimeoutError:
            processo.kill()
            await processo.wait()


def _colher_artefatos(trabalho: Path, declarados: list[str]) -> list[ArtefatoProduzido]:
    """Lê do diretório de trabalho os arquivos que a automação declarou.

    Só os declarados: varrer o diretório inteiro recolheria arquivo temporário,
    cache e o próprio `parametros.json`. A automação diz o que é entrega.
    """
    colhidos: list[ArtefatoProduzido] = []
    for caminho_relativo in declarados:
        if not caminho_relativo:
            continue
        destino = (trabalho / caminho_relativo).resolve()
        # Impede que `../../segredo` ou um link simbólico levem a leitura para
        # fora do diretório de trabalho.
        if not destino.is_relative_to(trabalho.resolve()):
            log.warning("artefato.fora_do_diretorio", caminho=caminho_relativo)
            continue
        if not destino.is_file():
            log.warning("artefato.declarado_mas_ausente", caminho=caminho_relativo)
            continue
        colhidos.append(
            ArtefatoProduzido(
                caminho_relativo=caminho_relativo,
                conteudo=destino.read_bytes(),
                tipo_conteudo=_TIPOS_POR_EXTENSAO.get(
                    destino.suffix.lower(), "application/octet-stream"
                ),
            )
        )
    return colhidos


def _descobrir_raiz_automacoes() -> Path:
    """Acha `automations/` subindo a partir deste arquivo.

    Funciona tanto com o pacote instalado em modo editável (o caso do
    desenvolvimento) quanto copiado para `/app` na imagem Docker.
    """
    if valor := os.getenv("ORBITA_DIR_AUTOMACOES"):
        return Path(valor)
    atual = Path(__file__).resolve()
    for pai in atual.parents:
        candidato = pai / "automations"
        if candidato.is_dir():
            return candidato
    return Path.cwd() / "automations"


def tipo_conteudo_por_extensao(nome: str) -> str:
    return _TIPOS_POR_EXTENSAO.get(Path(nome).suffix.lower(), "application/octet-stream")


def ambiente_permitido() -> tuple[str, ...]:
    """Exposto para o teste que garante que segredo não vaza para a automação."""
    return _AMBIENTE_PERMITIDO
