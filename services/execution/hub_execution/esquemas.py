"""Contratos de entrada e saída do `execution-service`."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from hub_core.executor.base import StatusExecucao


class PedidoDeDisparo(BaseModel):
    parametros: dict[str, Any] = Field(default_factory=dict)
    fila: str | None = None
    """Força uma fila específica. Normalmente é o catálogo que decide, a partir
    do perfil da automação."""


class PedidoDeDisparoPorSlug(PedidoDeDisparo):
    """O que a interface envia para `POST /execucoes`.

    Separado de `PedidoDeDisparo` para que `motor.disparar` receba o slug como
    argumento nomeado — o agendador dispara sem passar por HTTP e não deveria
    ter de montar um corpo de requisição para isso.
    """

    automacao: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,78}[a-z0-9]$")


class LinhaDeLog(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    sequencia: int = 0
    nivel: str = "INFO"
    mensagem: str
    ocorrido_em: datetime | None = None


class ResumoDeArtefato(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID | None = None
    nome_arquivo: str
    caminho_storage: str
    tamanho_bytes: int = 0
    tipo_conteudo: str = "application/octet-stream"
    sha256: str | None = None

    @property
    def tamanho_legivel(self) -> str:
        tamanho = float(self.tamanho_bytes)
        for unidade in ("B", "KB", "MB", "GB"):
            if tamanho < 1024 or unidade == "GB":
                return f"{tamanho:.0f} {unidade}" if unidade == "B" else f"{tamanho:.1f} {unidade}"
            tamanho /= 1024
        return f"{tamanho:.1f} GB"


class ExecucaoCartao(BaseModel):
    """A execução como aparece em lista. Sem log, sem parâmetros."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    automacao_slug: str
    automacao_nome: str
    automacao_versao: str | None = None
    usuario_nome: str = ""
    status: str
    status_rotulo: str = ""
    origem: str = "manual"
    fila: str = ""
    executor: str | None = None
    simulada: bool = False
    """Marcado quando o executor foi o simulado. A interface rotula o cartão —
    número de consumo simulado sem rótulo é a única parte de uma demonstração
    que seria desonesta."""
    enfileirada_em: datetime
    iniciada_em: datetime | None = None
    finalizada_em: datetime | None = None
    duracao_ms: int | None = None
    espera_ms: int | None = None
    resultado_resumo: str | None = None
    em_andamento: bool = False

    @property
    def duracao_legivel(self) -> str:
        if self.duracao_ms is None:
            return "—"
        segundos = self.duracao_ms / 1000
        if segundos < 1:
            return f"{self.duracao_ms} ms"
        if segundos < 60:
            return f"{segundos:.1f} s"
        minutos, resto = divmod(int(segundos), 60)
        return f"{minutos}min {resto}s"


class ExecucaoDetalhe(ExecucaoCartao):
    parametros: dict[str, Any] = Field(default_factory=dict)
    consumo: dict[str, Any] = Field(default_factory=dict)
    erro: str | None = None
    codigo_saida: int | None = None
    tentativa: int = 1
    trace_id: str | None = None
    logs: list[LinhaDeLog] = Field(default_factory=list)
    ultima_sequencia: int = 0
    """Maior `sequencia` devolvida. A interface manda este número de volta no
    próximo poll para receber só o que chegou depois."""
    artefatos: list[ResumoDeArtefato] = Field(default_factory=list)
    metricas: dict[str, float] = Field(default_factory=dict)


class PaginaDeExecucoes(BaseModel):
    itens: list[ExecucaoCartao]
    total: int
    pagina: int
    por_pagina: int


# ── Callbacks do runner ──────────────────────────────────────────────────────


class PedidoDeInicio(BaseModel):
    executor: str
    tentativa: int = 1


class PedidoDeLogs(BaseModel):
    linhas: list[LinhaDeLog] = Field(max_length=500)
    """Teto de 500 por requisição: protege o serviço de um lote gigante vindo
    de automação que entrou em laço emitindo log."""


class PedidoDeFinalizacao(BaseModel):
    status: StatusExecucao
    codigo_saida: int | None = None
    resumo: str = ""
    erro: str | None = None
    metricas: dict[str, float] = Field(default_factory=dict)
    artefatos: list[ResumoDeArtefato] = Field(default_factory=list)
    consumo: dict[str, Any] = Field(default_factory=dict)


# ── Indicadores ──────────────────────────────────────────────────────────────


class IndicadorDeFila(BaseModel):
    fila: str
    pendente: int = 0
    processando: int = 0
    concluida: int = 0
    morta: int = 0


class SerieDiaria(BaseModel):
    dia: str
    sucesso: int = 0
    falha: int = 0
    outras: int = 0


class AutomacaoMaisUsada(BaseModel):
    slug: str
    nome: str
    execucoes: int
    taxa_de_sucesso: float
    duracao_media_ms: int | None = None


class Indicadores(BaseModel):
    """O que o painel mostra.

    Cada campo abaixo corresponde a uma pergunta operacional concreta, e não a
    "um número bonito para o dashboard":

    * `ativas` e `filas` — a plataforma está dando conta agora?
    * `espera_media_ms` — a fila está acumulando?
    * `taxa_de_sucesso_7d` — alguma automação passou a falhar?
    * `serie_14d` — a tendência é de piora?
    """

    total: int = 0
    ativas: int = 0
    hoje: int = 0
    sucesso_7d: int = 0
    falha_7d: int = 0
    taxa_de_sucesso_7d: float = 0.0
    duracao_media_ms: int | None = None
    espera_media_ms: int | None = None
    filas: list[IndicadorDeFila] = Field(default_factory=list)
    serie_14d: list[SerieDiaria] = Field(default_factory=list)
    mais_usadas: list[AutomacaoMaisUsada] = Field(default_factory=list)
    agendamentos: list[AgendamentoResumo] = Field(default_factory=list)


class AgendamentoResumo(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    gatilho_id: uuid.UUID
    automacao_slug: str
    expressao: str
    descricao: str = ""
    fila: str = ""
    ultima_execucao_em: datetime | None = None
    proxima_em: datetime | None = None
    disparos: int = 0


Indicadores.model_rebuild()
