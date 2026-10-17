"""SDK de automação do Órbita, conveniência, não requisito.

O contrato da plataforma é imprimir linhas no stdout (ver
`hub_core.executor.base`). Isto funciona como automação completa:

    echo '@@orbita {"tipo":"log","nivel":"INFO","mensagem":"ola"}'
    echo '@@orbita {"tipo":"resumo","mensagem":"pronto"}'

Este pacote existe para que uma automação em Python não precise montar JSON na
mão, e para dar nomes às coisas:

    from hub_sdk import Orbita

    with Orbita() as orbita:
        orbita.info("lendo o cadastro")
        dados = carregar(orbita.parametro("competencia"))
        orbita.metrica("linhas_lidas", len(dados))
        orbita.artefato("relatorio.xlsx", planilha_em_bytes)
        orbita.resumo(f"{len(dados)} registros consolidados")

Um SDK equivalente em Node, Go ou Java tem umas 40 linhas, é só formatar JSON
e imprimir. É esse o ponto do contrato por stdout: não é preciso que a
plataforma suporte uma linguagem, basta que a linguagem saiba imprimir.

## O que o SDK garante que o `echo` não garante

* **flush imediato.** Sem `flush=True`, o Python armazena a saída em buffer
  quando o stdout não é um terminal, e é exatamente o caso dentro de um
  container. O log apareceria todo de uma vez no fim, destruindo o
  acompanhamento ao vivo. É o erro mais comum ao escrever a primeira
  automação;
* **artefato gravado no lugar certo.** O diretório de trabalho vem em
  `ORBITA_DIR_TRABALHO`, e é só de lá que o executor colhe arquivos;
* **falha reportada.** O `__exit__` captura exceção, emite um log de ERROR com
  o traceback e sai com código 1, em vez de o container morrer com um
  traceback cru que ninguém correlaciona com a execução.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path
from types import TracebackType
from typing import Any

__all__ = ["PREFIXO", "Orbita"]

PREFIXO = "@@orbita "


class Orbita:
    """Canal da automação com a plataforma."""

    def __init__(self, *, saida: Any = None) -> None:
        self._saida = saida or sys.stdout
        self.execucao_id = os.getenv("ORBITA_EXECUCAO_ID", "local")
        self.automacao = os.getenv("ORBITA_AUTOMACAO", "desconhecida")
        self.dir_trabalho = Path(os.getenv("ORBITA_DIR_TRABALHO", "."))
        self._parametros: dict[str, Any] | None = None
        self._resumo_enviado = False

    # ── Ciclo de vida ────────────────────────────────────────────────────────

    def __enter__(self) -> Orbita:
        self.debug(f"automação {self.automacao} iniciada (execução {self.execucao_id})")
        return self

    def __exit__(
        self,
        tipo: type[BaseException] | None,
        erro: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        if erro is not None:
            # O traceback vai para o log da execução, linha por linha, em vez
            # de ser impresso cru. Assim ele aparece na tela de quem disparou,
            # junto do resto do log, e não só no stdout do runner.
            self.erro(f"{type(erro).__name__}: {erro}")
            for linha in traceback.format_exception(tipo, erro, tb):
                for parte in linha.rstrip().splitlines():
                    self.erro(parte)
            if not self._resumo_enviado:
                self.resumo(f"Falhou: {type(erro).__name__}")
            # `raise SystemExit(1)` e não `return False`: garante código de
            # saída 1 mesmo que algum `atexit` interfira, e é o código de saída
            # que o executor lê para decidir que a execução falhou.
            self._enviar({"tipo": "log", "nivel": "ERROR", "mensagem": "encerrando com erro"})
            raise SystemExit(1)

        if not self._resumo_enviado:
            self.resumo("Execução concluída")
        return False

    # ── Protocolo ────────────────────────────────────────────────────────────

    def _enviar(self, dados: dict[str, Any]) -> None:
        linha = PREFIXO + json.dumps(dados, ensure_ascii=False, default=str)
        print(linha, file=self._saida, flush=True)

    def log(self, nivel: str, mensagem: str) -> None:
        self._enviar({"tipo": "log", "nivel": nivel.upper(), "mensagem": str(mensagem)})

    def debug(self, mensagem: str) -> None:
        self.log("DEBUG", mensagem)

    def info(self, mensagem: str) -> None:
        self.log("INFO", mensagem)

    def aviso(self, mensagem: str) -> None:
        self.log("WARNING", mensagem)

    def erro(self, mensagem: str) -> None:
        self.log("ERROR", mensagem)

    def metrica(self, nome: str, valor: float) -> None:
        """Publica uma métrica numérica da execução.

        Aparece na tela da execução e fica consultável no histórico. Para
        número de negócio ("linhas processadas", "valor glosado"), não para
        tempo ou memória, esses a plataforma mede sozinha.
        """
        self._enviar({"tipo": "metrica", "nome": str(nome), "valor": float(valor)})

    def resumo(self, mensagem: str) -> None:
        """Define a frase que aparece na lista de execuções.

        Uma linha, ≤ 500 caracteres. É o que alguém lê sem abrir a execução,
        então vale escrever o resultado ("1420 outorgas em 8 regiões") e não o
        estado ("processo finalizado").
        """
        self._resumo_enviado = True
        self._enviar({"tipo": "resumo", "mensagem": str(mensagem)[:500]})

    # ── Entrada e saída ──────────────────────────────────────────────────────

    @property
    def parametros(self) -> dict[str, Any]:
        """Os parâmetros da execução, já validados pela plataforma.

        Vêm por arquivo, e não por argumento de linha de comando, porque
        argumento de processo é visível em `ps` para qualquer usuário da
        máquina, e parâmetro de automação pode conter dado sensível.
        """
        if self._parametros is None:
            caminho = os.getenv("ORBITA_PARAMETROS")
            if caminho and Path(caminho).is_file():
                self._parametros = json.loads(Path(caminho).read_text(encoding="utf-8"))
            else:
                self._parametros = {}
        return self._parametros

    def parametro(self, nome: str, padrao: Any = None) -> Any:
        return self.parametros.get(nome, padrao)

    def exigir(self, nome: str) -> Any:
        """Lê um parâmetro obrigatório, falhando com mensagem útil se faltar.

        Não deveria acontecer, a plataforma valida os obrigatórios antes de
        enfileirar. Existe para o caso de a automação ser rodada à mão, fora
        da plataforma, durante o desenvolvimento.
        """
        if nome not in self.parametros:
            raise KeyError(
                f"parâmetro obrigatório ausente: {nome!r}. "
                f"Declare-o em automation.yaml ou informe-o em "
                f"{os.getenv('ORBITA_PARAMETROS', 'parametros.json')}"
            )
        return self.parametros[nome]

    def artefato(self, nome: str, conteudo: bytes | str) -> Path:
        """Grava um arquivo de saída e o declara à plataforma.

        Os dois passos são necessários e o SDK faz os dois: gravar no diretório
        de trabalho e **declarar** o nome. O executor só colhe o que foi
        declarado, varrer o diretório recolheria arquivo temporário e cache.
        """
        destino = self.dir_trabalho / nome
        destino.parent.mkdir(parents=True, exist_ok=True)

        if isinstance(conteudo, str):
            destino.write_text(conteudo, encoding="utf-8")
        else:
            destino.write_bytes(conteudo)

        self._enviar({"tipo": "artefato", "caminho": nome})
        self.info(f"artefato gerado: {nome} ({destino.stat().st_size} bytes)")
        return destino

    def segredo(self, nome: str, padrao: str | None = None) -> str | None:
        """Lê uma variável de ambiente declarada no manifesto.

        O valor é injetado pelo runner, a partir do ambiente **dele**, e nunca
        passou pelo banco nem pela API, ver
        `hub_runner.worker.Worker._resolver_segredos`.
        """
        return os.getenv(nome, padrao)
