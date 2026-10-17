"""Emissão e validação de JWT.

O `identity-service` emite; os outros três serviços apenas validam. Nenhum
serviço consulta o `identity-service` para autorizar uma requisição — tudo de
que precisam está nos *claims* assinados. É o que permite o `catalog-service`
continuar atendendo quando o `identity-service` está fora do ar.

A contrapartida é a de sempre em JWT: **revogação não é imediata**. Tirar um
papel de alguém só tem efeito quando o token dela expira. Por isso
`JWT_EXPIRE_MINUTES` é 60 e não 24 horas; para revogação imediata seria preciso
uma lista de bloqueio consultada a cada requisição, que é exatamente a chamada
síncrona que este desenho evita. A troca está registrada em docs/adr/0007.

## Caminho de migração para OIDC

Os claims abaixo usam os nomes padrão do OIDC onde existe equivalente (`sub`,
`name`, `email`, `iss`, `aud`, `exp`, `iat`, `jti`). Os específicos da
plataforma vão sob um *namespace* (`orbita/papeis`), que é a convenção usada
por Keycloak e Auth0 para claims customizados. Quando o `identity-service` for
substituído por um provedor OIDC, a função `validar_token` troca a verificação
de HMAC por JWKS e o resto do sistema não muda.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt

from hub_core.auth.rbac import Papel, Permissao, permissoes_de, tem_permissao
from hub_core.errors import NaoAutenticado
from hub_core.settings import Settings

EMISSOR = "orbita-hub/identity"
AUDIENCIA = "orbita-hub"

NS = "orbita"
CLAIM_PAPEIS = f"{NS}/papeis"
CLAIM_UNIDADE = f"{NS}/unidade"
CLAIM_UNIDADE_SIGLA = f"{NS}/unidade_sigla"
CLAIM_CPF = f"{NS}/cpf"
CLAIM_DEMO = f"{NS}/demo"
CLAIM_GRUPOS = f"{NS}/grupos"
"""Ids dos grupos da pessoa.

Vai no token, e não é consultado no `identity-service` na hora de autorizar,
porque é disso que o `catalog-service` precisa para resolver a permissão de
escopo `equipe` — e consultar o identity a cada verificação recriaria
exatamente a dependência síncrona que este desenho evita.

O custo é tamanho de token: cada grupo são 36 caracteres. Com os poucos grupos
por pessoa que o modelo organizacional produz, o JWT fica em torno de 700
bytes, confortável para um cookie. Se alguém pertencesse a dezenas de grupos, a
decisão teria de ser revista — e é por isso que o número é limitado em
`MAX_GRUPOS_NO_TOKEN`.
"""

MAX_GRUPOS_NO_TOKEN = 40


@dataclass(frozen=True, slots=True)
class Identidade:
    """Quem está fazendo a requisição, do ponto de vista de qualquer serviço."""

    id: uuid.UUID
    nome: str
    email: str
    cpf: str
    papeis: tuple[str, ...] = field(default_factory=tuple)
    unidade_id: uuid.UUID | None = None
    unidade_sigla: str = ""
    grupos: tuple[uuid.UUID, ...] = field(default_factory=tuple)
    demonstracao: bool = False
    """`True` quando a sessão veio do modo de demonstração pública."""

    def pode(self, permissao: Permissao | str) -> bool:
        return tem_permissao(self.papeis, permissao)

    @property
    def permissoes(self) -> frozenset[Permissao]:
        return permissoes_de(self.papeis)

    @property
    def e_admin(self) -> bool:
        return Papel.ADMIN.value in self.papeis

    @property
    def primeiro_nome(self) -> str:
        return self.nome.split(" ", 1)[0] if self.nome else ""

    @property
    def papel_principal(self) -> str:
        """O papel mais amplo que a pessoa tem, para exibição.

        A ordem de `Papel` vai do mais amplo ao mais restrito, então o primeiro
        encontrado é o que descreve melhor o que ela pode fazer.
        """
        for papel in Papel:
            if papel.value in self.papeis:
                return papel.rotulo
        return "Sem papel"


def emitir_token(identidade: Identidade, settings: Settings) -> tuple[str, datetime]:
    """Assina um token para a identidade. Devolve o token e quando ele expira."""
    agora = datetime.now(UTC)
    expira_em = agora + timedelta(minutes=settings.jwt_expire_minutes)

    payload: dict[str, Any] = {
        "sub": str(identidade.id),
        "name": identidade.nome,
        "email": identidade.email,
        "iss": EMISSOR,
        "aud": AUDIENCIA,
        "iat": int(agora.timestamp()),
        "exp": int(expira_em.timestamp()),
        # `jti` existe para que a auditoria possa amarrar várias ações à mesma
        # sessão sem guardar o token em si.
        "jti": uuid.uuid4().hex,
        CLAIM_PAPEIS: list(identidade.papeis),
        CLAIM_CPF: identidade.cpf,
        CLAIM_UNIDADE: str(identidade.unidade_id) if identidade.unidade_id else None,
        CLAIM_UNIDADE_SIGLA: identidade.unidade_sigla,
        CLAIM_GRUPOS: [str(g) for g in identidade.grupos[:MAX_GRUPOS_NO_TOKEN]],
        CLAIM_DEMO: identidade.demonstracao,
    }

    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, expira_em


def validar_token(token: str, settings: Settings) -> Identidade:
    """Verifica assinatura, prazo, emissor e audiência. Levanta `NaoAutenticado`.

    `algorithms` é uma lista fechada com um único item de propósito. Aceitar o
    algoritmo que o próprio token declara é a vulnerabilidade clássica de JWT:
    um token com `alg: none`, ou com `alg: HS256` quando o servidor esperava
    RS256 e usa a chave pública como segredo HMAC, passa a validar.
    """
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            audience=AUDIENCIA,
            issuer=EMISSOR,
            options={"require": ["exp", "iat", "sub", "aud", "iss"]},
        )
    except jwt.ExpiredSignatureError as erro:
        raise NaoAutenticado("sessão expirada") from erro
    except jwt.InvalidTokenError as erro:
        raise NaoAutenticado("token inválido") from erro

    unidade_bruta = payload.get(CLAIM_UNIDADE)

    try:
        return Identidade(
            id=uuid.UUID(payload["sub"]),
            nome=payload.get("name", ""),
            email=payload.get("email", ""),
            cpf=payload.get(CLAIM_CPF, ""),
            papeis=tuple(payload.get(CLAIM_PAPEIS) or ()),
            unidade_id=uuid.UUID(unidade_bruta) if unidade_bruta else None,
            unidade_sigla=payload.get(CLAIM_UNIDADE_SIGLA, ""),
            grupos=tuple(_uuids(payload.get(CLAIM_GRUPOS))),
            demonstracao=bool(payload.get(CLAIM_DEMO, False)),
        )
    except (KeyError, ValueError, TypeError) as erro:
        raise NaoAutenticado("token malformado") from erro


def _uuids(bruto: Any) -> list[uuid.UUID]:
    """Converte uma lista de strings em UUIDs, descartando o que não converte.

    Descartar em silêncio, e não levantar: um claim malformado deve reduzir o
    acesso da pessoa, não derrubar a requisição dela com erro 401 que não
    explica nada.
    """
    if not isinstance(bruto, list):
        return []
    convertidos: list[uuid.UUID] = []
    for item in bruto:
        try:
            convertidos.append(uuid.UUID(str(item)))
        except (ValueError, AttributeError, TypeError):
            continue
    return convertidos


def emitir_token_de_servico(servico: str, settings: Settings) -> str:
    """Token para chamada entre serviços (ex.: runner reportando execução).

    Vida curta (5 min) e sem identidade de pessoa: o sujeito é o serviço. A
    diferença importa na auditoria — "o runner alterou a execução" não é o
    mesmo registro que "a Maria alterou a execução".
    """
    agora = datetime.now(UTC)
    payload = {
        "sub": f"service:{servico}",
        "name": servico,
        "email": "",
        "iss": EMISSOR,
        "aud": AUDIENCIA,
        "iat": int(agora.timestamp()),
        "exp": int((agora + timedelta(minutes=5)).timestamp()),
        "jti": uuid.uuid4().hex,
        f"{NS}/servico": True,
    }
    return jwt.encode(payload, settings.internal_shared_secret, algorithm=settings.jwt_algorithm)


def validar_token_de_servico(token: str, settings: Settings) -> str:
    """Valida token de serviço e devolve o nome do serviço chamador."""
    try:
        payload = jwt.decode(
            token,
            settings.internal_shared_secret,
            algorithms=[settings.jwt_algorithm],
            audience=AUDIENCIA,
            issuer=EMISSOR,
        )
    except jwt.InvalidTokenError as erro:
        raise NaoAutenticado("token de serviço inválido") from erro

    if not payload.get(f"{NS}/servico"):
        raise NaoAutenticado("token não é de serviço")
    return str(payload.get("name", "desconhecido"))
