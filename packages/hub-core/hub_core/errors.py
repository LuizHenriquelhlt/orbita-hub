"""Erros de domínio da plataforma.

São deliberadamente agnósticos de HTTP: um serviço levanta `NaoEncontrado` e o
tradutor registrado em `hub_core.service` decide que isso vira um 404. Assim a
regra de negócio nunca precisa importar `fastapi`.
"""

from __future__ import annotations


class ErroOrbita(Exception):
    """Raiz de toda exceção esperada da plataforma."""

    codigo = "erro_interno"
    status_http = 500

    def __init__(self, mensagem: str, /, **detalhes: object) -> None:
        super().__init__(mensagem)
        self.mensagem = mensagem
        self.detalhes = detalhes


class NaoEncontrado(ErroOrbita):
    codigo = "nao_encontrado"
    status_http = 404


class Conflito(ErroOrbita):
    """O pedido é válido mas colide com o estado atual (ex.: slug duplicado)."""

    codigo = "conflito"
    status_http = 409


class EntradaInvalida(ErroOrbita):
    codigo = "entrada_invalida"
    status_http = 422


class NaoAutenticado(ErroOrbita):
    codigo = "nao_autenticado"
    status_http = 401


class SemPermissao(ErroOrbita):
    codigo = "sem_permissao"
    status_http = 403


class TransicaoInvalida(ErroOrbita):
    """Tentativa de mover uma automação para um estado inalcançável a partir do atual."""

    codigo = "transicao_invalida"
    status_http = 409


class LimiteDeConcorrencia(ErroOrbita):
    """A automação já está com o número máximo de execuções simultâneas."""

    codigo = "limite_de_concorrencia"
    status_http = 429


class DependenciaIndisponivel(ErroOrbita):
    """Um adaptador externo (fila, storage, executor) não respondeu."""

    codigo = "dependencia_indisponivel"
    status_http = 503


class ImagemIndisponivel(DependenciaIndisponivel):
    """O executor nao conseguiu obter a imagem da automacao.

    Subclasse de `DependenciaIndisponivel` e nao de um erro de automacao,
    porque e exatamente isso: a automacao nao teve a sua chance. O registry
    pode estar fora do ar, a credencial pode ter expirado, a tag pode ter sido
    removida. Nenhum desses casos e culpa do codigo da automacao, e todos
    podem passar sozinhos, entao a mensagem deve voltar para a fila.

    Tem classe propria para que o executor de reserva possa reconhece-la sem
    inspecionar texto de mensagem.
    """

    codigo = "imagem_indisponivel"
