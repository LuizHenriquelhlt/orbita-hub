"""Comunicação entre serviços — e o truque que faz um monorepo rodar de dois jeitos.

## O problema

A plataforma é quatro serviços. Em produção, são quatro processos que conversam
por HTTP. No free tier, não cabem quatro processos: não há memória para quatro
interpretadores Python, e um serviço gratuito que hiberna multiplicaria o
tempo de resposta a frio por quatro, em cascata.

A saída usual é manter dois códigos — um "modo dev monolítico" e um "modo
produção distribuído" — que divergem em um mês e passam a esconder bugs um do
outro.

## A solução

`httpx` tem um transporte, `ASGITransport`, que em vez de abrir socket chama a
aplicação ASGI diretamente, em memória. Como é um transporte, ele troca a
*camada de transporte* sem mudar a *camada de aplicação*: o código que chama
continua sendo

    resposta = await cliente.get(f"/automacoes/{slug}")

e continua recebendo um `httpx.Response` com status, cabeçalhos e corpo. O que
muda é que nenhum byte atravessa a rede. Middleware, validação do Pydantic,
dependências, serialização JSON e tradução de erro continuam todos
funcionando — a requisição realmente passa pela pilha do FastAPI do serviço
destino.

Então o modo de composição é uma linha de configuração:

    HUB_MODE=distributed  ->  httpx.AsyncClient(base_url="http://catalog:8002")
    HUB_MODE=monolith     ->  httpx.AsyncClient(transport=ASGITransport(app))

E `services/execution/` não tem um único `if` sobre o modo.

## Duas armadilhas

**`ASGITransport` não executa o *lifespan*.** Ele chama a aplicação com escopo
de requisição, nunca com o de inicialização. Os eventos de `startup` do serviço
embutido simplesmente não rodam, e a primeira requisição falha em um
`engine()` não inicializado. Quem monta o monolito precisa rodar os lifespans
— é o que `hub_gateway.main` faz, com `contextlib.AsyncExitStack`.

**Erro em HTTP não é exceção.** Um 404 do `catalog-service` chega como resposta
com status 404, não como `NaoEncontrado`. `ClienteServico` traduz de volta, de
modo que quem chama trata erro de domínio do mesmo jeito nos dois modos.
"""

from __future__ import annotations

from typing import Any

import httpx

from hub_core.errors import (
    Conflito,
    DependenciaIndisponivel,
    EntradaInvalida,
    ErroOrbita,
    LimiteDeConcorrencia,
    NaoAutenticado,
    NaoEncontrado,
    SemPermissao,
    TransicaoInvalida,
)
from hub_core.logging import obter_logger, obter_trace_id
from hub_core.settings import Settings

log = obter_logger(__name__)

IDENTITY = "identity"
CATALOG = "catalog"
EXECUTION = "execution"
RUNNER = "runner"

SERVICOS = (IDENTITY, CATALOG, EXECUTION, RUNNER)

# Preenchido por `hub_gateway.main` no modo monolito. Em modo distribuído fica
# vazio e os clientes usam URL de rede.
_APPS_EMBUTIDAS: dict[str, Any] = {}

_ERRO_POR_STATUS: dict[int, type[ErroOrbita]] = {
    401: NaoAutenticado,
    403: SemPermissao,
    404: NaoEncontrado,
    409: Conflito,
    422: EntradaInvalida,
    429: LimiteDeConcorrencia,
    503: DependenciaIndisponivel,
}


def registrar_app_embutida(nome: str, app: Any) -> None:
    """Registra a aplicação ASGI de um serviço para chamada in-process."""
    if nome not in SERVICOS:
        raise ValueError(f"serviço desconhecido: {nome!r}")
    _APPS_EMBUTIDAS[nome] = app


def limpar_apps_embutidas() -> None:
    _APPS_EMBUTIDAS.clear()


def _url_base(nome: str, settings: Settings) -> str:
    return {
        IDENTITY: settings.identity_service_url,
        CATALOG: settings.catalog_service_url,
        EXECUTION: settings.execution_service_url,
        RUNNER: settings.runner_service_url,
    }[nome]


def construir_cliente_http(
    nome: str, settings: Settings, *, timeout: float = 30.0
) -> httpx.AsyncClient:
    """Cria o `AsyncClient` apontado para um serviço irmão.

    É aqui, e só aqui, que o modo de composição aparece.
    """
    if settings.modo_monolito:
        app = _APPS_EMBUTIDAS.get(nome)
        if app is None:
            raise RuntimeError(
                f"HUB_MODE=monolith mas o serviço {nome!r} não foi registrado. "
                "Chame hub_core.clients.registrar_app_embutida() antes de usar o cliente."
            )
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            # O host é fictício: nada resolve DNS, mas o httpx exige uma base
            # válida para montar caminho relativo.
            base_url=f"http://{nome}.interno",
            timeout=timeout,
        )

    return httpx.AsyncClient(
        base_url=_url_base(nome, settings),
        timeout=timeout,
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
    )


class ClienteServico:
    """Cliente de um serviço irmão, com tradução de erro e propagação de contexto.

    Instanciado uma vez por processo e guardado no `app.state`: criar um
    `AsyncClient` por requisição descarta o pool de conexões e transforma cada
    chamada em um handshake TCP novo.
    """

    def __init__(self, nome: str, settings: Settings, *, timeout: float = 30.0) -> None:
        self.nome = nome
        self._settings = settings
        self._cliente = construir_cliente_http(nome, settings, timeout=timeout)

    async def aclose(self) -> None:
        await self._cliente.aclose()

    def _cabecalhos(self, token: str | None, extras: dict[str, str] | None) -> dict[str, str]:
        cabecalhos: dict[str, str] = {"accept": "application/json"}
        if token:
            cabecalhos["authorization"] = f"Bearer {token}"
        # Propagar o trace id é o que torna possível seguir uma requisição que
        # passou por três serviços lendo um log só.
        if trace := obter_trace_id():
            cabecalhos["x-trace-id"] = trace
        if extras:
            cabecalhos.update(extras)
        return cabecalhos

    async def requisitar(
        self,
        metodo: str,
        caminho: str,
        *,
        token: str | None = None,
        json: Any = None,
        params: dict[str, Any] | None = None,
        cabecalhos: dict[str, str] | None = None,
    ) -> Any:
        """Faz a chamada e devolve o JSON, ou levanta o erro de domínio equivalente."""
        try:
            resposta = await self._cliente.request(
                metodo,
                caminho,
                json=json,
                params=_limpar_params(params),
                headers=self._cabecalhos(token, cabecalhos),
            )
        except httpx.TimeoutException as erro:
            raise DependenciaIndisponivel(
                f"{self.nome} não respondeu no prazo", servico=self.nome, caminho=caminho
            ) from erro
        except httpx.HTTPError as erro:
            raise DependenciaIndisponivel(
                f"falha ao falar com {self.nome}", servico=self.nome, detalhe=str(erro)
            ) from erro

        if resposta.is_success:
            if resposta.status_code == 204 or not resposta.content:
                return None
            return resposta.json()

        self._levantar(resposta, caminho)

    def _levantar(self, resposta: httpx.Response, caminho: str) -> None:
        """Converte a resposta de erro de volta em exceção de domínio."""
        try:
            corpo = resposta.json()
        except ValueError:
            corpo = {}

        detalhe = corpo.get("detail") if isinstance(corpo, dict) else None
        mensagem = (
            detalhe.get("mensagem")
            if isinstance(detalhe, dict)
            else (detalhe or f"{self.nome} respondeu {resposta.status_code}")
        )
        detalhes = detalhe.get("detalhes", {}) if isinstance(detalhe, dict) else {}
        codigo = detalhe.get("codigo") if isinstance(detalhe, dict) else None

        # `transicao_invalida` e `conflito` compartilham o 409; o código no
        # corpo é o que permite distinguir os dois ao reconstruir a exceção.
        if codigo == "transicao_invalida":
            classe: type[ErroOrbita] = TransicaoInvalida
        else:
            classe = _ERRO_POR_STATUS.get(resposta.status_code, ErroOrbita)

        log.warning(
            "cliente.erro",
            servico=self.nome,
            caminho=caminho,
            status=resposta.status_code,
            mensagem=str(mensagem)[:200],
        )
        raise classe(str(mensagem), servico=self.nome, **detalhes)

    # Atalhos legíveis.
    async def get(self, caminho: str, **kwargs: Any) -> Any:
        return await self.requisitar("GET", caminho, **kwargs)

    async def post(self, caminho: str, **kwargs: Any) -> Any:
        return await self.requisitar("POST", caminho, **kwargs)

    async def patch(self, caminho: str, **kwargs: Any) -> Any:
        return await self.requisitar("PATCH", caminho, **kwargs)

    async def delete(self, caminho: str, **kwargs: Any) -> Any:
        return await self.requisitar("DELETE", caminho, **kwargs)

    async def saudavel(self) -> bool:
        try:
            await self.get("/saude")
            return True
        except ErroOrbita:
            return False


_clientes_brutos: dict[str, httpx.AsyncClient] = {}


def cliente_http_bruto(nome: str, settings: Settings | None = None) -> httpx.AsyncClient:
    """Cliente `httpx` cru para um serviço, sem tradução de erro.

    `ClienteServico` decodifica JSON e converte status de erro em exceção de
    domínio, que é o que a maioria das chamadas quer. Há dois casos em que isso
    estorva:

    * **a resposta não é JSON** — o download de um artefato é um arquivo
      binário, e tentar `resposta.json()` nele falha;
    * **o redirecionamento precisa ser inspecionado, não seguido** — quando o
      armazenamento é S3, o serviço responde 307 para uma URL pré-assinada, e
      quem chama quer repassar esse redirecionamento ao navegador em vez de
      baixar o arquivo e retransmiti-lo.

    Funciona nos dois modos de composição, como `ClienteServico`: em monolito,
    por `ASGITransport`.
    """
    if nome not in _clientes_brutos:
        from hub_core.settings import obter_settings

        _clientes_brutos[nome] = construir_cliente_http(
            nome, settings or obter_settings(), timeout=120.0
        )
    return _clientes_brutos[nome]


async def encerrar_clientes_brutos() -> None:
    for cliente in _clientes_brutos.values():
        await cliente.aclose()
    _clientes_brutos.clear()


def _limpar_params(params: dict[str, Any] | None) -> dict[str, Any] | None:
    """Remove chaves com valor `None`.

    Sem isto, um filtro opcional não informado viaja como `?status=None` e o
    serviço destino filtra pela string literal "None", devolvendo lista vazia
    sem erro — o tipo de bug que custa uma tarde.
    """
    if not params:
        return None
    return {chave: valor for chave, valor in params.items() if valor is not None}
