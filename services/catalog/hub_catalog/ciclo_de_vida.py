"""Ciclo de vida de uma automação, como máquina de estados explícita.

    rascunho ──► homologacao ──► publicada ──► depreciada ──► arquivada
        ▲             │              │                            │
        └─────────────┘              └────────────────────────────┘
          (reprovada)                      (republicação)

## Por que uma tabela de transições, e não `if` espalhado

A regra "automação em rascunho não pode ir direto para publicada" precisa valer
em todo caminho que muda status: a rota de aprovação, a importação de
manifesto, o script de seed e o reprocessamento administrativo. Com `if` em
cada um, o quarto caminho esquece a regra, e o estado inválido só aparece
depois, como uma automação publicada que nunca passou por homologação.

Aqui existe um dicionário. Quem quiser mudar status chama `validar_transicao`,
e não há outro caminho para a coluna `status`.

## Por que publicar exige requisitos, e não só permissão

`requisitos_para_publicar` devolve a lista do que falta. A alternativa, um
booleano, produz a mensagem "não é possível publicar", que faz a pessoa
abrir um chamado. A lista produz "falta README e um responsável técnico", que
ela resolve sozinha.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, NamedTuple

from hub_core.errors import TransicaoInvalida

if TYPE_CHECKING:
    from hub_catalog.modelos import Automacao


class StatusAutomacao(StrEnum):
    RASCUNHO = "rascunho"
    HOMOLOGACAO = "homologacao"
    PUBLICADA = "publicada"
    DEPRECIADA = "depreciada"
    ARQUIVADA = "arquivada"

    @property
    def rotulo(self) -> str:
        return {
            StatusAutomacao.RASCUNHO: "Rascunho",
            StatusAutomacao.HOMOLOGACAO: "Em homologação",
            StatusAutomacao.PUBLICADA: "Publicada",
            StatusAutomacao.DEPRECIADA: "Depreciada",
            StatusAutomacao.ARQUIVADA: "Arquivada",
        }[self]

    @property
    def descricao(self) -> str:
        return {
            StatusAutomacao.RASCUNHO: "Em construção. Só quem é dono vê e testa.",
            StatusAutomacao.HOMOLOGACAO: "Aguardando aprovação do gestor.",
            StatusAutomacao.PUBLICADA: "No catálogo, disponível para quem tem acesso.",
            StatusAutomacao.DEPRECIADA: (
                "Substituída. Continua executável, mas sinalizada no catálogo."
            ),
            StatusAutomacao.ARQUIVADA: "Fora de operação. Não executa.",
        }[self]

    @property
    def executavel(self) -> bool:
        """Automação arquivada não roda; depreciada ainda roda, com aviso.

        Manter a depreciada executável é intencional: ela costuma ser
        substituída por outra que ainda está em homologação, e cortar a
        execução no momento da depreciação deixaria um intervalo sem nenhuma
        das duas funcionando.
        """
        return self in {
            StatusAutomacao.RASCUNHO,
            StatusAutomacao.HOMOLOGACAO,
            StatusAutomacao.PUBLICADA,
            StatusAutomacao.DEPRECIADA,
        }

    @property
    def visivel_no_catalogo(self) -> bool:
        return self in {StatusAutomacao.PUBLICADA, StatusAutomacao.DEPRECIADA}

    @property
    def cor(self) -> str:
        """Token de cor usado pelos templates. Mantido junto do estado para que
        acrescentar um estado não exija caçar um `switch` no CSS."""
        return {
            StatusAutomacao.RASCUNHO: "neutro",
            StatusAutomacao.HOMOLOGACAO: "atencao",
            StatusAutomacao.PUBLICADA: "sucesso",
            StatusAutomacao.DEPRECIADA: "aviso",
            StatusAutomacao.ARQUIVADA: "inativo",
        }[self]


class Transicao(NamedTuple):
    de: StatusAutomacao
    para: StatusAutomacao
    acao: str
    """Verbo que aparece no botão da interface."""
    permissao: str
    """Permissão exigida, no formato de `hub_core.auth.rbac.Permissao`."""


TRANSICOES: tuple[Transicao, ...] = (
    Transicao(
        StatusAutomacao.RASCUNHO,
        StatusAutomacao.HOMOLOGACAO,
        "Enviar para homologação",
        "automacao:editar",
    ),
    Transicao(
        StatusAutomacao.HOMOLOGACAO,
        StatusAutomacao.RASCUNHO,
        "Reprovar e devolver",
        "automacao:publicar",
    ),
    Transicao(
        StatusAutomacao.HOMOLOGACAO,
        StatusAutomacao.PUBLICADA,
        "Aprovar e publicar",
        "automacao:publicar",
    ),
    Transicao(
        StatusAutomacao.PUBLICADA,
        StatusAutomacao.DEPRECIADA,
        "Depreciar",
        "automacao:publicar",
    ),
    # Permite desfazer uma depreciação feita por engano sem recriar a
    # automação, e sem passar de novo por homologação, já que a versão
    # publicada é a mesma que já havia sido aprovada.
    Transicao(
        StatusAutomacao.DEPRECIADA,
        StatusAutomacao.PUBLICADA,
        "Republicar",
        "automacao:publicar",
    ),
    Transicao(
        StatusAutomacao.DEPRECIADA,
        StatusAutomacao.ARQUIVADA,
        "Arquivar",
        "automacao:arquivar",
    ),
    Transicao(
        StatusAutomacao.RASCUNHO,
        StatusAutomacao.ARQUIVADA,
        "Descartar",
        "automacao:arquivar",
    ),
)

# Índice para consulta rápida, montado uma vez.
_PERMITIDAS: dict[tuple[StatusAutomacao, StatusAutomacao], Transicao] = {
    (t.de, t.para): t for t in TRANSICOES
}


def transicoes_de(status: StatusAutomacao) -> tuple[Transicao, ...]:
    """O que se pode fazer a partir deste estado. Alimenta os botões da tela."""
    return tuple(t for t in TRANSICOES if t.de is status)


def validar_transicao(de: StatusAutomacao, para: StatusAutomacao) -> Transicao:
    """Devolve a transição ou levanta `TransicaoInvalida` explicando o caminho.

    A mensagem de erro nomeia os destinos possíveis. Sem isso, a resposta é
    "transição inválida", e quem integra via API fica adivinhando.
    """
    if de is para:
        raise TransicaoInvalida(
            f"a automação já está em {de.rotulo.lower()}",
            status_atual=de.value,
        )
    transicao = _PERMITIDAS.get((de, para))
    if transicao is None:
        possiveis = [t.para.value for t in transicoes_de(de)]
        raise TransicaoInvalida(
            f"não é possível ir de {de.rotulo.lower()} para {para.rotulo.lower()}",
            status_atual=de.value,
            status_pedido=para.value,
            destinos_possiveis=possiveis,
        )
    return transicao


def requisitos_para_publicar(automacao: Automacao) -> list[str]:
    """O que falta para a automação poder ser publicada. Lista vazia = pronta.

    Reproduz, no que é verificável sem infraestrutura externa, a porta de
    qualidade descrita na arquitetura-alvo. O que **não** é verificado aqui, e
    por isso está dito em voz alta: *scan* de vulnerabilidade da imagem. Ele
    exige um registry com scanner acoplado; no lugar, o pipeline de CI do
    repositório roda `trivy` na imagem das automações de exemplo. Ver
    docs/adr/0008.
    """
    faltando: list[str] = []

    if not (automacao.descricao or "").strip():
        faltando.append("descrição preenchida")
    if len((automacao.descricao or "").strip()) < 40:
        faltando.append("descrição com ao menos 40 caracteres")
    if not (automacao.readme or "").strip():
        faltando.append("documentação (README)")
    if not (automacao.responsavel_tecnico or "").strip():
        faltando.append("responsável técnico")
    if not (automacao.responsavel_negocio or "").strip():
        faltando.append("responsável de negócio")
    if not automacao.imagem:
        faltando.append("imagem declarada")
    if not automacao.versoes:
        faltando.append("ao menos uma versão registrada")
    if automacao.max_execucoes_concorrentes < 1:
        faltando.append("limite de execuções concorrentes maior que zero")

    return faltando
