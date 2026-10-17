"""Autenticação e autorização: senhas, JWT, RBAC e dependências do FastAPI."""

from hub_core.auth.dependencias import (
    ServicoChamador,
    UsuarioAtual,
    UsuarioOpcional,
    chamador_interno,
    exigir,
    identidade_atual,
    identidade_opcional,
)
from hub_core.auth.rbac import (
    PERMISSOES_POR_PAPEL,
    Papel,
    Permissao,
    mascarar_cpf,
    mascarar_email,
    permissoes_de,
    tem_permissao,
)
from hub_core.auth.senhas import gerar_hash, precisa_rehash, verificar
from hub_core.auth.tokens import (
    Identidade,
    emitir_token,
    emitir_token_de_servico,
    validar_token,
    validar_token_de_servico,
)

__all__ = [
    "PERMISSOES_POR_PAPEL",
    "Identidade",
    "Papel",
    "Permissao",
    "ServicoChamador",
    "UsuarioAtual",
    "UsuarioOpcional",
    "chamador_interno",
    "emitir_token",
    "emitir_token_de_servico",
    "exigir",
    "gerar_hash",
    "identidade_atual",
    "identidade_opcional",
    "mascarar_cpf",
    "mascarar_email",
    "permissoes_de",
    "precisa_rehash",
    "tem_permissao",
    "validar_token",
    "validar_token_de_servico",
    "verificar",
]
