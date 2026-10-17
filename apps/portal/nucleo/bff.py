"""A camada BFF: como o portal fala com os quatro serviços.

O navegador conversa **só** com o Django. O Django chama os serviços do lado
servidor, com o JWT que guarda na sessão. Duas consequências:

* **o token nunca chega ao navegador.** Não há JWT em `localStorage`, o alvo
  preferido de XSS em aplicação com API. O que o navegador tem é um cookie
  `HttpOnly` assinado;
* **a API interna não precisa ser pública.** Em Kubernetes, os quatro serviços
  ficam em rede interna, sem Ingress. O único artefato exposto é este.

## Reuso do cliente do núcleo

`nucleo.bff` não implementa HTTP: usa `hub_core.clients.ClienteServico`, o
mesmo que os serviços usam entre si. Com isso o portal herda de graça a
tradução de erro HTTP para exceção de domínio, a propagação de `trace_id` e,
o que mais importa, **o modo monolito**: em `HUB_MODE=monolith` as chamadas
vão por `ASGITransport`, sem rede, com o mesmo código de view.

## Por que as views são assíncronas

`ASGITransport` é assíncrono. Para o modo monolito funcionar, a chamada tem de
ser `await`. Django 5 suporta view assíncrona de ponta a ponta sob ASGI, então
a escolha é só escrever `async def`.

O ganho vale além do monolito: a tela de uma automação faz duas chamadas
independentes (ficha no catálogo, estatísticas no motor). Com views
assíncronas, `asyncio.gather` as faz em paralelo e a página responde no tempo
da mais lenta, não na soma das duas.
"""

from __future__ import annotations

import asyncio
from typing import Any

from django.conf import settings

from hub_core.clients import CATALOG, EXECUTION, IDENTITY, RUNNER, ClienteServico
from hub_core.errors import ErroOrbita, NaoAutenticado
from hub_core.settings import obter_settings

# Um cliente por serviço, por processo. Criar um por requisição descartaria o
# pool de conexões e transformaria cada chamada em um handshake TCP novo.
_clientes: dict[str, ClienteServico] = {}


def cliente(servico: str) -> ClienteServico:
    if servico not in _clientes:
        cfg = obter_settings()
        _clientes[servico] = ClienteServico(servico, cfg, timeout=30.0)
    return _clientes[servico]


async def encerrar_clientes() -> None:
    for existente in _clientes.values():
        await existente.aclose()
    _clientes.clear()


class Bff:
    """Fachada das chamadas que o portal faz, por domínio.

    Métodos nomeados em vez de `get("/automacoes")` espalhado pelas views: o
    caminho de cada endpoint aparece **uma vez** no projeto. Quando o
    `catalog-service` renomear uma rota, há um lugar para corrigir.
    """

    def __init__(self, token: str | None) -> None:
        self._token = token

    # ── Identidade ───────────────────────────────────────────────────────────

    async def login(self, cpf: str, senha: str) -> dict[str, Any]:
        return await cliente(IDENTITY).post(
            "/autenticacao/login", json={"cpf": cpf, "senha": senha}
        )

    async def login_demonstracao(self, papel: str) -> dict[str, Any]:
        return await cliente(IDENTITY).post("/autenticacao/demonstracao", json={"papel": papel})

    async def eu(self) -> dict[str, Any]:
        return await cliente(IDENTITY).get("/autenticacao/eu", token=self._token)

    async def papeis(self) -> list[dict[str, Any]]:
        """Rota aberta: alimenta o seletor de papel da demonstração."""
        return await cliente(IDENTITY).get("/autenticacao/papeis")

    async def usuarios(self, **filtros: Any) -> dict[str, Any]:
        return await cliente(IDENTITY).get("/usuarios", token=self._token, params=filtros)

    async def usuario(self, usuario_id: str) -> dict[str, Any]:
        return await cliente(IDENTITY).get(f"/usuarios/{usuario_id}", token=self._token)

    async def unidades(self) -> list[dict[str, Any]]:
        return await cliente(IDENTITY).get("/unidades", token=self._token)

    async def grupos(self) -> list[dict[str, Any]]:
        return await cliente(IDENTITY).get("/grupos", token=self._token)

    async def auditoria(self, **filtros: Any) -> dict[str, Any]:
        return await cliente(IDENTITY).get("/auditoria", token=self._token, params=filtros)

    # ── Catálogo ─────────────────────────────────────────────────────────────

    async def catalogo(self, **filtros: Any) -> dict[str, Any]:
        return await cliente(CATALOG).get("/automacoes", token=self._token, params=filtros)

    async def automacao(self, slug: str) -> dict[str, Any]:
        return await cliente(CATALOG).get(f"/automacoes/{slug}", token=self._token)

    async def criar_automacao(self, dados: dict[str, Any]) -> dict[str, Any]:
        return await cliente(CATALOG).post("/automacoes", token=self._token, json=dados)

    async def transicionar(self, slug: str, para: str, justificativa: str = "") -> dict[str, Any]:
        return await cliente(CATALOG).post(
            f"/automacoes/{slug}/transicao",
            token=self._token,
            json={"para": para, "justificativa": justificativa or None},
        )

    async def importar_manifesto(self, yaml: str, publicar: bool = False) -> dict[str, Any]:
        return await cliente(CATALOG).post(
            "/manifestos/importar",
            token=self._token,
            json={"manifesto_yaml": yaml, "publicar": publicar},
        )

    # ── Execuções ────────────────────────────────────────────────────────────

    async def disparar(
        self, slug: str, parametros: dict[str, Any], fila: str | None = None
    ) -> dict[str, Any]:
        return await cliente(EXECUTION).post(
            "/execucoes",
            token=self._token,
            json={"automacao": slug, "parametros": parametros, "fila": fila},
        )

    async def execucoes(self, **filtros: Any) -> dict[str, Any]:
        return await cliente(EXECUTION).get("/execucoes", token=self._token, params=filtros)

    async def execucao(self, execucao_id: str, desde_sequencia: int = 0) -> dict[str, Any]:
        return await cliente(EXECUTION).get(
            f"/execucoes/{execucao_id}",
            token=self._token,
            params={"desde_sequencia": desde_sequencia},
        )

    async def cancelar(self, execucao_id: str) -> dict[str, Any]:
        return await cliente(EXECUTION).post(
            f"/execucoes/{execucao_id}/cancelar", token=self._token
        )

    async def reexecutar(self, execucao_id: str) -> dict[str, Any]:
        return await cliente(EXECUTION).post(
            f"/execucoes/{execucao_id}/reexecutar", token=self._token
        )

    async def indicadores(self) -> dict[str, Any]:
        return await cliente(EXECUTION).get("/indicadores", token=self._token)

    async def estatisticas(self, slug: str) -> dict[str, Any]:
        return await cliente(EXECUTION).get(f"/automacoes/{slug}/estatisticas", token=self._token)

    async def filas(self) -> list[dict[str, Any]]:
        return await cliente(EXECUTION).get("/filas", token=self._token)

    async def mensagens_mortas(self, limite: int = 50) -> list[dict[str, Any]]:
        return await cliente(EXECUTION).get(
            "/filas/mortas", token=self._token, params={"limite": limite}
        )

    async def reviver_mensagem(self, mensagem_id: str) -> dict[str, Any]:
        return await cliente(EXECUTION).post(
            f"/filas/mortas/{mensagem_id}/reviver", token=self._token
        )

    # ── Runner ───────────────────────────────────────────────────────────────

    async def situacao_do_runner(self) -> dict[str, Any]:
        return await cliente(RUNNER).get("/situacao", token=self._token)

    async def containers(self) -> list[dict[str, Any]]:
        return await cliente(RUNNER).get("/containers", token=self._token)

    # ── Composição ───────────────────────────────────────────────────────────

    async def ficha_completa(self, slug: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """Ficha da automação e estatísticas dela, em paralelo.

        São dois serviços diferentes, `catalog` sabe o que a automação é,
        `execution` sabe como ela tem se comportado. É exatamente o custo da
        separação entre serviços, e `gather` faz a página pagar o tempo da
        chamada mais lenta em vez da soma das duas.

        As estatísticas são opcionais: se o motor estiver fora do ar, a ficha
        ainda abre, sem os números. Uma página que não carrega porque um
        serviço secundário caiu é pior do que uma página incompleta.
        """
        ficha, estatisticas = await asyncio.gather(
            self.automacao(slug),
            self.estatisticas(slug),
            return_exceptions=True,
        )
        if isinstance(ficha, BaseException):
            raise ficha
        if isinstance(estatisticas, BaseException):
            estatisticas = {}
        return ficha, estatisticas  # type: ignore[return-value]


def de(request: Any) -> Bff:
    """Cria o BFF a partir da requisição, pegando o token da sessão."""
    return Bff(request.session.get("token"))


def modo_demonstracao() -> bool:
    return bool(settings.DEMO_MODE)


__all__ = ["Bff", "ErroOrbita", "NaoAutenticado", "de", "encerrar_clientes", "modo_demonstracao"]
