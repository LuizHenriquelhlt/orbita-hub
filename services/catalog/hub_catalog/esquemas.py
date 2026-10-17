"""Contratos de entrada e saída do `catalog-service`."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from hub_catalog.ciclo_de_vida import StatusAutomacao
from hub_catalog.manifesto import (
    GatilhoDeclarado,
    ParametroDeclarado,
    Recursos,
    VariavelDeAmbiente,
)


class AcaoDisponivel(BaseModel):
    """Uma transição de estado que quem está olhando pode executar agora.

    O serviço devolve isto já filtrado pelas permissões de quem pediu, em vez
    de a interface recalcular a matriz de transições. Assim o botão que aparece
    na tela é exatamente o que a API vai aceitar, não existe o caso de um
    botão que devolve 403 ao ser clicado.
    """

    para: str
    acao: str
    permissao: str


class VersaoResumo(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    versao: str
    tag_imagem: str = ""
    changelog: str | None = None
    criado_em: datetime


class GatilhoResumo(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tipo: str
    expressao: str | None = None
    fila: str
    ativo: bool
    parametros_padrao: dict[str, Any] = Field(default_factory=dict)


class PermissaoResumo(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    escopo: str
    escopo_id: uuid.UUID | None = None
    nivel: str


class AutomacaoCartao(BaseModel):
    """A automação como ela aparece na listagem do catálogo.

    Enxuta de propósito: a listagem traz dezenas de itens, e `readme`,
    `roteiro_simulado` e `manifesto` somados passam de 100 KB por automação.
    Mandar isso para montar um cartão com título e etiqueta é o jeito mais
    rápido de deixar a página inicial lenta.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    slug: str
    nome: str
    descricao: str
    categoria: str
    tipo: str
    status: str
    status_rotulo: str = ""
    status_cor: str = ""
    tags: list[str] = Field(default_factory=list)
    modo_visual: bool = False
    versao_atual: str | None = None
    publicado_em: date | None = None
    pode_executar: bool = False
    """Resolvido para quem pediu: papel × permissões declaradas da automação."""


class AutomacaoDetalhe(AutomacaoCartao):
    readme: str | None = None
    responsavel_tecnico: str | None = None
    responsavel_negocio: str | None = None
    unidade_dona_id: uuid.UUID | None = None
    imagem: str = ""
    entrypoint: list[str] = Field(default_factory=list)
    rede_habilitada: bool = False
    max_execucoes_concorrentes: int = 1
    limites: Recursos = Field(default_factory=Recursos)
    consumo_estimado: dict[str, Any] = Field(default_factory=dict)
    parametros: list[ParametroDeclarado] = Field(default_factory=list)
    variaveis_ambiente: list[VariavelDeAmbiente] = Field(default_factory=list)
    versoes: list[VersaoResumo] = Field(default_factory=list)
    gatilhos: list[GatilhoResumo] = Field(default_factory=list)
    permissoes: list[PermissaoResumo] = Field(default_factory=list)
    acoes_disponiveis: list[AcaoDisponivel] = Field(default_factory=list)
    requisitos_pendentes: list[str] = Field(default_factory=list)
    criado_em: datetime | None = None
    atualizado_em: datetime | None = None


class PaginaDeAutomacoes(BaseModel):
    itens: list[AutomacaoCartao]
    total: int
    pagina: int
    por_pagina: int
    facetas: dict[str, dict[str, int]] = Field(default_factory=dict)
    """Contagem por categoria e por tipo, para os filtros do catálogo.

    Vem junto com a página em vez de em um endpoint separado: a interface
    precisa dos dois ao mesmo tempo, e duas chamadas significariam duas
    varreduras da mesma tabela com os mesmos filtros.
    """


class PedidoDeCriacao(BaseModel):
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,78}[a-z0-9]$")
    nome: str = Field(min_length=3, max_length=160)
    descricao: str = Field(default="", max_length=4000)
    categoria: str
    tipo: str
    responsavel_tecnico: str | None = Field(default=None, max_length=160)
    responsavel_negocio: str | None = Field(default=None, max_length=160)
    unidade_dona_id: uuid.UUID | None = None
    imagem: str = Field(default="", max_length=300)
    entrypoint: list[str] = Field(default_factory=list)
    readme: str | None = None
    modo_visual: bool = False
    rede_habilitada: bool = False
    max_execucoes_concorrentes: int = Field(default=1, ge=1, le=50)
    limites: Recursos = Field(default_factory=Recursos)
    parametros: list[ParametroDeclarado] = Field(default_factory=list)
    variaveis_ambiente: list[VariavelDeAmbiente] = Field(default_factory=list)
    gatilhos: list[GatilhoDeclarado] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list, max_length=12)


class PedidoDeAtualizacao(BaseModel):
    """Atualização parcial: só o que vem no corpo é alterado.

    `model_fields_set` distingue "campo ausente" de "campo enviado como
    `null`". Sem essa distinção, um PATCH que manda apenas `{"nome": "X"}`
    apagaria `descricao`, `readme` e todo o resto, o bug clássico de PATCH
    implementado como PUT.
    """

    nome: str | None = Field(default=None, min_length=3, max_length=160)
    descricao: str | None = Field(default=None, max_length=4000)
    categoria: str | None = None
    tipo: str | None = None
    responsavel_tecnico: str | None = None
    responsavel_negocio: str | None = None
    imagem: str | None = None
    entrypoint: list[str] | None = None
    readme: str | None = None
    modo_visual: bool | None = None
    rede_habilitada: bool | None = None
    max_execucoes_concorrentes: int | None = Field(default=None, ge=1, le=50)
    limites: Recursos | None = None
    parametros: list[ParametroDeclarado] | None = None
    variaveis_ambiente: list[VariavelDeAmbiente] | None = None
    tags: list[str] | None = None

    def alteracoes(self) -> dict[str, Any]:
        """Só os campos que o cliente realmente enviou."""
        return self.model_dump(exclude_unset=True, mode="json")


class PedidoDeTransicao(BaseModel):
    para: StatusAutomacao
    justificativa: str | None = Field(default=None, max_length=500)


class PedidoDeNovaVersao(BaseModel):
    versao: str = Field(pattern=r"^\d+\.\d+\.\d+(-[0-9a-z.]+)?$")
    tag_imagem: str = Field(default="", max_length=300)
    changelog: str | None = Field(default=None, max_length=2000)


class PedidoDeImportacaoDeManifesto(BaseModel):
    manifesto_yaml: str = Field(max_length=200_000)
    publicar: bool = False
    """Se `true` e quem pede tem `automacao:publicar`, a automação importada
    já entra publicada em vez de rascunho. Usado pelo seed e pelo `orbita
    publicar`, não pela interface."""


class ResultadoDeImportacao(BaseModel):
    slug: str
    criada: bool
    versao: str
    status: str
    avisos: list[str] = Field(default_factory=list)


class PedidoDePreparacao(BaseModel):
    """Corpo de `POST /automacoes/{slug}/preparar-execucao`."""

    parametros: dict[str, Any] = Field(default_factory=dict)
    fila: str | None = None


class ChecagemDeAcesso(BaseModel):
    """Resposta de `POST /automacoes/{slug}/preparar-execucao`.

    Consumida pelo `execution-service` antes de enfileirar. Responde a duas
    perguntas em uma chamada:

    1. **esta pessoa pode disparar esta automação?** O motor já sabe que o
       papel dela permite disparar execuções; é o catálogo que sabe se ela tem
       acesso a *esta* automação (ver `hub_core.auth.rbac` sobre as duas
       camadas);
    2. **os parâmetros informados são válidos?** A declaração é do catálogo,
       então a validação também, o motor receber `parametros_saneados` já
       convertidos evita que as duas pontas tenham cada uma a sua ideia do que
       é uma "competência válida".

    Juntar as duas em um `POST` em vez de dois `GET` tem um motivo prático: no
    modo distribuído, são dois round-trips a menos no caminho mais quente da
    plataforma.
    """

    permitido: bool
    motivo: str = ""
    nivel: Literal["visualizar", "executar", "configurar", "nenhum"] = "nenhum"
    automacao_id: uuid.UUID | None = None
    versao_id: uuid.UUID | None = None
    versao: str | None = None
    imagem: str = ""
    entrypoint: list[str] = Field(default_factory=list)
    limites: Recursos = Field(default_factory=Recursos)
    rede_habilitada: bool = False
    max_execucoes_concorrentes: int = 1
    parametros: list[ParametroDeclarado] = Field(default_factory=list)
    variaveis_ambiente: list[VariavelDeAmbiente] = Field(default_factory=list)
    roteiro_simulado: list[dict[str, Any]] = Field(default_factory=list)
    fila_sugerida: str = "execucoes.padrao"
    parametros_saneados: dict[str, Any] = Field(default_factory=dict)
    """Os parâmetros já validados e convertidos, contendo **apenas** as chaves
    declaradas. É o que o motor persiste e passa ao runner."""
    nome: str = ""
    slug: str = ""
