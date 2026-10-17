"""Hash de senha com bcrypt.

Usa a biblioteca `bcrypt` direto, sem `passlib`. O `passlib` é a escolha
tradicional, mas está sem release desde 2020 e a versão 1.7.4 quebra com
`bcrypt>=4.1` (ele lê `bcrypt.__about__.__version__`, atributo que foi
removido). A API de que se precisa aqui são duas funções; a camada de
abstração não paga o custo da dependência parada.

`bcrypt` e não `argon2` por um motivo de contexto: o sistema real que inspira
este projeto já tem base de usuários com hash bcrypt, e migrar algoritmo de
hash exige reescrever o hash no próximo login de cada pessoa. Para um projeto
novo, `argon2id` seria a escolha melhor — e `verificar` abaixo está preparado
para conviver com os dois durante uma migração.
"""

from __future__ import annotations

import bcrypt

# Custo 12: ~250 ms por verificação em hardware de 2026. O suficiente para
# tornar ataque de dicionário caro sem transformar o login em gargalo quando
# várias pessoas entram ao mesmo tempo no começo do expediente.
CUSTO_BCRYPT = 12

# Limite do bcrypt: ele ignora tudo além do 72º byte. Senha mais longa não é
# rejeitada — é truncada em silêncio, o que faz duas senhas diferentes com o
# mesmo prefixo de 72 bytes passarem a ser a mesma senha. Aqui o limite é
# verificado e informado.
MAX_BYTES_SENHA = 72


class SenhaMuitoLonga(ValueError):
    pass


def gerar_hash(senha: str) -> str:
    bytes_senha = senha.encode("utf-8")
    if len(bytes_senha) > MAX_BYTES_SENHA:
        raise SenhaMuitoLonga(
            f"a senha tem {len(bytes_senha)} bytes; o bcrypt usa no máximo {MAX_BYTES_SENHA}"
        )
    return bcrypt.hashpw(bytes_senha, bcrypt.gensalt(rounds=CUSTO_BCRYPT)).decode("utf-8")


def verificar(senha: str, hash_armazenado: str) -> bool:
    """Confere a senha. Devolve `False` em vez de levantar, em qualquer erro.

    Hash corrompido, truncado ou de algoritmo desconhecido tem de resultar em
    "senha errada", não em erro 500 — caso contrário a mensagem de erro
    distingue "usuário existe com hash estranho" de "usuário não existe", e
    isso é enumeração de conta.
    """
    try:
        bytes_senha = senha.encode("utf-8")[:MAX_BYTES_SENHA]
        return bcrypt.checkpw(bytes_senha, hash_armazenado.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def precisa_rehash(hash_armazenado: str) -> bool:
    """Diz se o hash foi gerado com custo menor que o atual.

    Chamado depois de um login bem-sucedido: se o custo subiu desde que a senha
    foi definida, é o momento de regravar com o custo novo, já que a senha em
    texto claro está em mãos exatamente ali e em nenhum outro lugar.
    """
    try:
        partes = hash_armazenado.split("$")
        return int(partes[2]) < CUSTO_BCRYPT
    except (IndexError, ValueError):
        return True
