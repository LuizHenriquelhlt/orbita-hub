"""O manifesto de automação: o contrato de extensão da plataforma.

Nenhuma automação nova exige alteração no código do Órbita. Ela é uma imagem
(ou um diretório, no executor de subprocesso) mais um `automation.yaml`
versionado junto do próprio código da automação.

## O formato

O envelope é o de um recurso do Kubernetes — `apiVersion`, `kind`, `metadata`,
`spec`. Não por mimetismo: é uma convenção que a maioria de quem opera
plataforma reconhece de imediato, e que já resolve versionamento de schema
(`apiVersion`) sem inventar nada.

Os nomes de campo dentro do envelope são em português, como o resto do
projeto. A mistura é deliberada: a estrutura segue uma convenção conhecida, o
vocabulário segue o domínio.

    apiVersion: orbita.inrh/v1
    kind: Automacao
    metadata:
      nome: relatorio-outorgas-mensal
      titulo: Relatório mensal de outorgas
      versao: 1.4.0
      categoria: relatorios
      tags: [relatorio, outorgas]
      responsavel_tecnico: Núcleo de Dados
      responsavel_negocio: Coordenação de Outorgas
    spec:
      tipo: batch
      imagem: local/relatorio-outorgas-mensal
      entrypoint: [python, -I, main.py]
      recursos: { cpus: 0.5, memoria_mb: 512, timeout_segundos: 600 }
      parametros: [...]
      gatilhos: [...]
      permissoes: [...]

## Por que Pydantic e não JSON Schema escrito à mão

O projeto precisa de três coisas a partir do mesmo contrato: validar o
manifesto recebido, gerar o formulário de parâmetros no portal, e publicar um
schema para autocompletar no editor de quem escreve a automação.

Escrever JSON Schema à mão dá o terceiro e deixa os dois primeiros como código
duplicado que sai de sincronia. Com Pydantic, a validação é o modelo, e
`model_json_schema()` gera o JSON Schema — uma fonte, três usos. O schema
publicado em `docs/automation-schema.json` é gerado, não mantido.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from hub_catalog.modelos import CATEGORIAS, TIPOS_DE_AUTOMACAO
from hub_core.errors import EntradaInvalida

API_VERSION = "orbita.inrh/v1"
KIND = "Automacao"

Slug = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{1,78}[a-z0-9]$", strip_whitespace=True),
]
SemVer = Annotated[str, StringConstraints(pattern=r"^\d+\.\d+\.\d+(-[0-9a-z.]+)?$")]
Tag = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]*$", max_length=40)]

TipoDeParametro = Literal[
    "texto",
    "texto_longo",
    "numero_inteiro",
    "numero_decimal",
    "booleano",
    "data",
    "mes",
    "opcao",
    "multiopcao",
]


class OpcaoDeParametro(BaseModel):
    model_config = ConfigDict(extra="forbid")

    valor: str
    rotulo: str


class ParametroDeclarado(BaseModel):
    """Um parâmetro de entrada da automação.

    O portal gera o campo do formulário a partir disto, e o
    `execution-service` valida o que foi enviado contra isto. Ter os dois lados
    lendo a mesma declaração é o que impede a divergência clássica: um campo
    que a interface oferece e o motor rejeita.
    """

    model_config = ConfigDict(extra="forbid")

    nome: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,39}$")]
    rotulo: str = Field(max_length=120)
    tipo: TipoDeParametro = "texto"
    obrigatorio: bool = False
    ajuda: str | None = Field(default=None, max_length=300)
    padrao: Any = None
    opcoes: list[OpcaoDeParametro] = Field(default_factory=list)
    minimo: float | None = None
    maximo: float | None = None
    mascara: str | None = Field(default=None, max_length=60)
    """Dica de formato exibida no campo (ex.: `AAAA-MM`). Não é validação."""

    @model_validator(mode="after")
    def _opcoes_coerentes(self) -> ParametroDeclarado:
        """`opcao`/`multiopcao` exigem opções; os outros tipos não as aceitam.

        Validador **de modelo**, não de campo, e por um motivo que custa um
        bug para descobrir: no Pydantic v2 um `@field_validator` não roda
        quando o campo cai no valor padrão. Um manifesto com `tipo: opcao` e
        sem a chave `opcoes` nunca passaria pelo validador de `opcoes`, e a
        automação seria aceita com um campo de seleção sem nenhuma alternativa.

        `mode="after"` roda sempre, com o modelo já montado e os dois campos
        em mão. A regra geral que se tira disso: validação que envolve mais de
        um campo pertence ao modelo.
        """
        exige_opcoes = self.tipo in {"opcao", "multiopcao"}
        if exige_opcoes and not self.opcoes:
            raise ValueError(f"parâmetro do tipo '{self.tipo}' precisa de ao menos uma opção")
        if not exige_opcoes and self.opcoes:
            raise ValueError(f"parâmetro do tipo '{self.tipo}' não aceita opções")

        if exige_opcoes:
            valores = [o.valor for o in self.opcoes]
            if len(valores) != len(set(valores)):
                raise ValueError("as opções têm valores repetidos")
        return self


class VariavelDeAmbiente(BaseModel):
    """Variável que a automação espera receber.

    `segredo_ref` é o **nome** do segredo, nunca o valor. O valor é resolvido
    pelo runner, a partir do ambiente dele, no momento de montar o container —
    e nunca passa pelo banco, pela API nem pela interface. Um manifesto com
    valor de segredo é recusado por `_sem_valor_de_segredo`.
    """

    model_config = ConfigDict(extra="forbid")

    nome: Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{0,59}$")]
    obrigatorio: bool = False
    descricao: str | None = Field(default=None, max_length=200)
    segredo_ref: str | None = Field(default=None, max_length=80)

    valor: Any = Field(
        default=None,
        exclude=True,
        description="Não use. Existe apenas para recusar o manifesto com mensagem clara.",
    )
    """Declarado como `Any`, e não como `None`, de propósito.

    Com `valor: None`, o Pydantic recusa sozinho — mas com a mensagem genérica
    "Input should be None", que não diz a quem escreveu o manifesto o que
    fazer. Aceitando `Any` e recusando no validador, a mensagem explica o
    porquê e aponta a alternativa. Vale a troca: esta é a mensagem de erro que
    mais gente vai ler ao escrever a primeira automação.
    """

    @field_validator("valor")
    @classmethod
    def _sem_valor_de_segredo(cls, valor: Any) -> None:
        if valor is not None:
            raise ValueError(
                "o manifesto não aceita 'valor' em variáveis de ambiente — "
                "use 'segredo_ref' com o nome do segredo e configure o valor "
                "no ambiente do runner, para que ele não trafegue pela API nem "
                "fique no banco"
            )
        return None


class Recursos(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cpus: float = Field(default=0.5, gt=0, le=8)
    memoria_mb: int = Field(default=512, ge=64, le=8192)
    timeout_segundos: int = Field(default=900, ge=5, le=14400)
    disco_mb: int = Field(default=256, ge=16, le=4096)


class GatilhoDeclarado(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tipo: Literal["manual", "cron", "webhook", "api"] = "manual"
    expressao: str | None = Field(default=None, max_length=120)
    fila: Literal["execucoes.interativa", "execucoes.padrao", "execucoes.lote"] = "execucoes.padrao"
    parametros_padrao: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _expressao_coerente_com_tipo(self) -> GatilhoDeclarado:
        """Cron exige expressão; webhook exige caminho; manual não aceita nenhum.

        Também de modelo, e não de campo, pelo mesmo motivo descrito em
        `ParametroDeclarado._opcoes_coerentes`: um gatilho `cron` que
        simplesmente omite `expressao` jamais acionaria um validador do campo
        `expressao`, e entraria no catálogo como agendamento que nunca
        dispara.
        """
        if self.tipo == "cron":
            if not self.expressao:
                raise ValueError("gatilho do tipo 'cron' exige 'expressao'")
            # Validação estrutural: cinco campos. A semântica de cada campo
            # fica em `hub_execution.agenda`, que é quem calcula a próxima
            # ocorrência — duplicar o parser aqui criaria duas definições de
            # cron que divergiriam.
            if len(self.expressao.split()) != 5:
                raise ValueError(
                    "expressão cron deve ter 5 campos: minuto hora dia mês dia-da-semana"
                )
        elif self.tipo == "webhook":
            if not self.expressao:
                raise ValueError("gatilho do tipo 'webhook' exige 'expressao' com o caminho")
            if not self.expressao.startswith("/"):
                raise ValueError("o caminho do webhook deve começar com '/'")
        elif self.expressao:
            raise ValueError(f"gatilho do tipo '{self.tipo}' não aceita 'expressao'")
        return self


class PermissaoDeclarada(BaseModel):
    model_config = ConfigDict(extra="forbid")

    escopo: Literal["usuario", "equipe", "unidade", "todos"] = "todos"
    identificador: str | None = None
    """Sigla da unidade ou nome do grupo. Resolvido para id na importação —
    manifesto com UUID cravado não sobrevive a um ambiente novo."""
    nivel: Literal["visualizar", "executar", "configurar"] = "executar"


class MetadadosDoManifesto(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nome: Slug
    titulo: str = Field(max_length=160)
    versao: SemVer
    categoria: Literal[*CATEGORIAS]  # type: ignore[valid-type]
    descricao: str = Field(default="", max_length=4000)
    tags: list[Tag] = Field(default_factory=list, max_length=12)
    responsavel_tecnico: str | None = Field(default=None, max_length=160)
    responsavel_negocio: str | None = Field(default=None, max_length=160)
    unidade: str | None = Field(default=None, max_length=20)
    changelog: str | None = Field(default=None, max_length=2000)


class EspecificacaoDoManifesto(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tipo: Literal[*TIPOS_DE_AUTOMACAO]  # type: ignore[valid-type]
    imagem: str = Field(max_length=300)
    entrypoint: list[str] = Field(default_factory=list, max_length=20)
    modo_visual: bool = False
    rede_habilitada: bool = False
    recursos: Recursos = Field(default_factory=Recursos)
    max_execucoes_concorrentes: int = Field(default=1, ge=1, le=50)
    consumo_estimado: dict[str, Any] = Field(default_factory=dict)
    parametros: list[ParametroDeclarado] = Field(default_factory=list, max_length=30)
    variaveis_ambiente: list[VariavelDeAmbiente] = Field(default_factory=list, max_length=30)
    gatilhos: list[GatilhoDeclarado] = Field(default_factory=list, max_length=10)
    permissoes: list[PermissaoDeclarada] = Field(default_factory=list, max_length=50)
    readme: str | None = None
    roteiro_simulado: list[dict[str, Any]] = Field(default_factory=list, max_length=40)

    @field_validator("parametros")
    @classmethod
    def _nomes_unicos(cls, valor: list[ParametroDeclarado]) -> list[ParametroDeclarado]:
        nomes = [p.nome for p in valor]
        duplicados = {n for n in nomes if nomes.count(n) > 1}
        if duplicados:
            raise ValueError(f"parâmetros com nome repetido: {sorted(duplicados)}")
        return valor


class Manifesto(BaseModel):
    """Um `automation.yaml` validado."""

    model_config = ConfigDict(extra="forbid")

    apiVersion: Literal[API_VERSION] = API_VERSION  # noqa: N815 - convenção do envelope
    kind: Literal[KIND] = KIND
    metadata: MetadadosDoManifesto
    spec: EspecificacaoDoManifesto

    @property
    def slug(self) -> str:
        return self.metadata.nome


def carregar(texto: str) -> Manifesto:
    """Interpreta e valida um manifesto em YAML.

    `yaml.safe_load` e não `yaml.load`: o loader completo instancia objetos
    Python arbitrários a partir de tags como `!!python/object/apply`, o que
    transforma "enviar um manifesto" em "executar código no servidor". É a
    vulnerabilidade mais comum em quem aceita YAML de fora.
    """
    try:
        dados = yaml.safe_load(texto)
    except yaml.YAMLError as erro:
        raise EntradaInvalida("YAML inválido", detalhe=str(erro)[:400]) from erro

    if not isinstance(dados, dict):
        raise EntradaInvalida("o manifesto deve ser um mapeamento no nível raiz")

    try:
        return Manifesto.model_validate(dados)
    except Exception as erro:
        raise EntradaInvalida(
            "manifesto não atende ao contrato", problemas=_resumir_erros(erro)
        ) from erro


def _resumir_erros(erro: Exception) -> list[dict[str, str]]:
    """Transforma o `ValidationError` do Pydantic em algo exibível em tela."""
    problemas = getattr(erro, "errors", None)
    if not callable(problemas):
        return [{"campo": "", "problema": str(erro)[:300]}]
    return [
        {
            "campo": ".".join(str(parte) for parte in item.get("loc", ())),
            "problema": str(item.get("msg", ""))[:300],
        }
        for item in problemas()
    ][:25]


def gerar_json_schema() -> dict[str, Any]:
    """JSON Schema do manifesto, derivado do modelo.

    Publicado em `docs/automation-schema.json` por `scripts/gerar_schema.py`.
    Quem escreve uma automação aponta o editor para ele e ganha
    autocompletar e validação enquanto digita — sem a plataforma precisar
    manter um segundo contrato à mão.
    """
    esquema = Manifesto.model_json_schema(mode="validation")
    esquema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    esquema["$id"] = "https://github.com/orbita-hub/docs/automation-schema.json"
    esquema["title"] = "Manifesto de automação Órbita"
    return esquema
