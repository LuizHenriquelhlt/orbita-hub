"""Validação e formatação de CPF.

Fica em módulo próprio porque é regra de domínio reutilizada em três lugares
(entrada de login, cadastro de usuário e geração dos dados de demonstração) e
porque é a única parte deste serviço que merece teste exaustivo.

## Sobre os CPFs da demonstração

Todos os CPFs do cenário fictício são gerados por `gerar_cpf_ficticio`, que
produz números com dígitos verificadores **matematicamente válidos** a partir
de uma semente conhecida. Isso é necessário para que o fluxo de validação seja
exercitado de verdade na demonstração, um CPF inválido seria recusado no
login e a tela nunca abriria.

Número válido não significa número de alguém: o espaço de CPFs válidos é de
~10^9 e a Receita Federal emitiu uma fração dele. Ainda assim, por precaução, a
geração usa o prefixo `000`, que não é atribuído a pessoa física real, é o
mesmo motivo pelo qual `000.000.000-00` aparece em manual de sistema.
"""

from __future__ import annotations

import random

TAMANHO = 11


def apenas_digitos(valor: str) -> str:
    return "".join(c for c in valor if c.isdigit())


def _digito_verificador(digitos: str, peso_inicial: int) -> int:
    """Calcula um dígito verificador pelo módulo 11 com pesos decrescentes."""
    soma = sum(int(d) * peso for d, peso in zip(digitos, range(peso_inicial, 1, -1), strict=True))
    resto = (soma * 10) % 11
    return 0 if resto == 10 else resto


def valido(valor: str) -> bool:
    """Confere os dois dígitos verificadores.

    Rejeita sequências de dígito repetido (`111.111.111-11`): elas passam no
    cálculo do módulo 11 por coincidência aritmética, e é a primeira coisa que
    se digita para testar um formulário.
    """
    digitos = apenas_digitos(valor)
    if len(digitos) != TAMANHO:
        return False
    if digitos == digitos[0] * TAMANHO:
        return False

    primeiro = _digito_verificador(digitos[:9], 10)
    if primeiro != int(digitos[9]):
        return False
    segundo = _digito_verificador(digitos[:10], 11)
    return segundo == int(digitos[10])


def normalizar(valor: str) -> str:
    """Devolve só os dígitos, validando. Levanta `ValueError` se inválido."""
    digitos = apenas_digitos(valor)
    if not valido(digitos):
        raise ValueError("CPF inválido")
    return digitos


def formatar(valor: str) -> str:
    """`12345678900` -> `123.456.789-00`."""
    d = apenas_digitos(valor)
    if len(d) != TAMANHO:
        return valor
    return f"{d[:3]}.{d[3:6]}.{d[6:9]}-{d[9:]}"


def gerar_cpf_ficticio(semente: int | str) -> str:
    """CPF com dígitos verificadores válidos, determinístico pela semente.

    Prefixo `000` por precaução (ver docstring do módulo). Determinístico para
    que o `seed` do cenário fictício produza sempre o mesmo conjunto, o que
    torna a demonstração reproduzível e os testes estáveis.
    """
    aleatorio = random.Random(f"orbita-cpf-{semente}")
    base = "000" + "".join(str(aleatorio.randint(0, 9)) for _ in range(6))
    primeiro = _digito_verificador(base, 10)
    segundo = _digito_verificador(base + str(primeiro), 11)
    return f"{base}{primeiro}{segundo}"
