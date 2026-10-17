"""Interpretação de expressões cron e cálculo da próxima ocorrência.

## Por que não uma biblioteca

`croniter` e `apscheduler` fazem isto bem, e usar um dos dois seria
defensável. A escolha de implementar aqui tem dois motivos concretos:

* **o subconjunto necessário é pequeno**, cinco campos, com `*`, número,
  faixa, lista e passo. Não há `@yearly`, não há segundos, não há `L`/`#` de
  cron estendido. São as ~120 linhas abaixo, sem dependência;
* **o comportamento fica explícito e testável**, em especial a semântica de
  dia-do-mês × dia-da-semana, que é a parte do cron que quase todo mundo
  implementa errado (ver `_dia_corresponde`).

Se o projeto precisasse de `@reboot`, de segundos ou de cron estendido, a
decisão correta seria trocar isto por `croniter`. Está registrado em
docs/adr/0009.

## Fuso horário

As expressões são interpretadas no fuso de `FUSO_PADRAO`, não em UTC. Quem
agenda "0 6 * * *" quer dizer seis da manhã no horário de quem vai receber o
relatório, não 3h da manhã local porque o servidor está em UTC.

O horário de verão é a complicação clássica desse tipo de cálculo, uma hora
que não existe na virada, uma que acontece duas vezes. Aqui o risco é menor por
um fato do domínio: o Brasil não adota horário de verão desde 2019, então
`America/Sao_Paulo` tem deslocamento fixo. Ainda assim, o cálculo é feito com
`zoneinfo` sobre datas conscientes de fuso, não com aritmética de offset, que
é o que quebraria caso o horário de verão volte.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

FUSO_PADRAO = ZoneInfo("America/Sao_Paulo")

# Limite de minutos examinados ao procurar a próxima ocorrência. Quatro anos
# cobre `0 0 29 2 *` (29 de fevereiro), a expressão válida com o maior
# intervalo possível entre ocorrências.
_MAX_MINUTOS = 4 * 366 * 24 * 60

# Faixa aceita por campo. Dia-da-semana vai até **7**, não 6: o cron aceita
# tanto `0` quanto `7` para domingo, e recusar o `7` quebraria expressões
# legítimas copiadas de um crontab. A normalização de 7 para 0 acontece depois
# da validação de faixa, em `_interpretar_campo`, fazê-la antes exigiria
# converter cada item da lista antes de saber se é válido.
_FAIXAS = (
    ("minuto", 0, 59),
    ("hora", 0, 23),
    ("dia", 1, 31),
    ("mes", 1, 12),
    ("dia_da_semana", 0, 7),
)


class ExpressaoCronInvalida(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Cron:
    """Uma expressão cron já interpretada, como conjuntos de valores válidos."""

    minutos: frozenset[int]
    horas: frozenset[int]
    dias: frozenset[int]
    meses: frozenset[int]
    dias_da_semana: frozenset[int]
    expressao: str

    dia_restrito: bool = True
    """`True` se o campo dia-do-mês não é `*`. Ver `_dia_corresponde`."""
    semana_restrita: bool = True
    """`True` se o campo dia-da-semana não é `*`."""

    def corresponde(self, momento: datetime) -> bool:
        return (
            momento.minute in self.minutos
            and momento.hour in self.horas
            and momento.month in self.meses
            and self._dia_corresponde(momento)
        )

    def _dia_corresponde(self, momento: datetime) -> bool:
        """A regra de dia do cron, que é a parte contraintuitiva.

        Quando **ambos** os campos de dia estão restritos, a relação é **OU**,
        não **E**. `0 0 1 * 1` dispara no dia 1º do mês *e também* em toda
        segunda-feira, não apenas no dia 1º que cair numa segunda.

        É assim desde o `cron` original do Unix e está no POSIX. Implementar
        como **E** produz um agendamento que quase nunca dispara, e o erro
        passa despercebido por meses, porque não gera falha, só silêncio.

        Quando só um dos dois está restrito, vale o restrito. Quando nenhum
        está, qualquer dia serve.
        """
        no_dia = momento.day in self.dias
        # `weekday()` do Python: 0 = segunda. No cron, 0 = domingo. A
        # conversão é `(weekday + 1) % 7`.
        dia_cron = (momento.weekday() + 1) % 7
        na_semana = dia_cron in self.dias_da_semana

        if self.dia_restrito and self.semana_restrita:
            return no_dia or na_semana
        if self.dia_restrito:
            return no_dia
        if self.semana_restrita:
            return na_semana
        return True

    def proxima_ocorrencia(
        self, *, depois_de: datetime | None = None, fuso: ZoneInfo = FUSO_PADRAO
    ) -> datetime | None:
        """Primeiro momento após `depois_de` em que a expressão dispara, em UTC.

        A busca avança minuto a minuto, mas pula o dia inteiro quando o dia não
        corresponde, o que reduz o pior caso de ~2 milhões de iterações para
        algumas centenas. É mais simples e mais fácil de conferir do que o
        cálculo campo a campo, e o custo é irrelevante para algo que roda uma
        vez por minuto.
        """
        inicio = (depois_de or datetime.now(UTC)).astimezone(fuso)
        # Zera os segundos e avança um minuto: a próxima ocorrência é
        # estritamente depois do momento dado.
        momento = inicio.replace(second=0, microsecond=0) + timedelta(minutes=1)

        examinados = 0
        while examinados < _MAX_MINUTOS:
            if momento.month not in self.meses or not self._dia_corresponde(momento):
                # Salta para a meia-noite do dia seguinte.
                proximo_dia = (momento + timedelta(days=1)).replace(hour=0, minute=0)
                examinados += max(1, int((proximo_dia - momento).total_seconds() // 60))
                momento = proximo_dia
                continue

            if momento.hour not in self.horas:
                proxima_hora = (momento + timedelta(hours=1)).replace(minute=0)
                examinados += max(1, int((proxima_hora - momento).total_seconds() // 60))
                momento = proxima_hora
                continue

            if momento.minute in self.minutos:
                return momento.astimezone(UTC)

            momento += timedelta(minutes=1)
            examinados += 1

        return None

    def descrever(self) -> str:
        """Descrição em português para a interface.

        Cobre os formatos frequentes com texto natural e cai para a expressão
        crua nos casos complexos, melhor mostrar `*/7 3 * * 2` do que uma
        frase gerada mecanicamente e confusa.
        """
        if self.expressao == "* * * * *":
            return "a cada minuto"

        um_minuto = len(self.minutos) == 1
        uma_hora = len(self.horas) == 1
        todo_dia = not self.dia_restrito and not self.semana_restrita
        todo_mes = len(self.meses) == 12

        if um_minuto and uma_hora and todo_dia and todo_mes:
            return f"todos os dias às {next(iter(self.horas)):02d}:{next(iter(self.minutos)):02d}"

        if um_minuto and uma_hora and self.dia_restrito and len(self.dias) == 1 and todo_mes:
            dia = next(iter(self.dias))
            return f"todo dia {dia} às {next(iter(self.horas)):02d}:{next(iter(self.minutos)):02d}"

        if um_minuto and uma_hora and self.semana_restrita and not self.dia_restrito:
            nomes = ("domingo", "segunda", "terça", "quarta", "quinta", "sexta", "sábado")
            dias = ", ".join(nomes[d] for d in sorted(self.dias_da_semana))
            return f"{dias} às {next(iter(self.horas)):02d}:{next(iter(self.minutos)):02d}"

        return self.expressao


def interpretar(expressao: str) -> Cron:
    """Interpreta uma expressão cron de cinco campos."""
    campos = expressao.split()
    if len(campos) != 5:
        raise ExpressaoCronInvalida(
            f"esperados 5 campos (minuto hora dia mês dia-da-semana), "
            f"recebidos {len(campos)}: {expressao!r}"
        )

    conjuntos: list[frozenset[int]] = []
    for campo, (nome, minimo, maximo) in zip(campos, _FAIXAS, strict=True):
        conjuntos.append(_interpretar_campo(campo, nome, minimo, maximo))

    return Cron(
        minutos=conjuntos[0],
        horas=conjuntos[1],
        dias=conjuntos[2],
        meses=conjuntos[3],
        dias_da_semana=conjuntos[4],
        expressao=" ".join(campos),
        dia_restrito=campos[2] != "*",
        semana_restrita=campos[4] != "*",
    )


def _interpretar_campo(campo: str, nome: str, minimo: int, maximo: int) -> frozenset[int]:
    """Interpreta um campo: `*`, `n`, `a-b`, `*/n`, `a-b/n` e listas com vírgula."""
    valores: set[int] = set()

    for parte in campo.split(","):
        if not parte:
            raise ExpressaoCronInvalida(f"campo '{nome}' tem item vazio em {campo!r}")

        corpo, _, passo_bruto = parte.partition("/")
        passo = 1
        if _:
            try:
                passo = int(passo_bruto)
            except ValueError as erro:
                raise ExpressaoCronInvalida(f"passo inválido em '{nome}': {parte!r}") from erro
            if passo < 1:
                raise ExpressaoCronInvalida(f"passo deve ser ≥ 1 em '{nome}': {parte!r}")

        if corpo == "*":
            inicio, fim = minimo, maximo
        elif "-" in corpo.lstrip("-"):
            inicio_bruto, _, fim_bruto = corpo.partition("-")
            inicio, fim = _inteiro(inicio_bruto, nome), _inteiro(fim_bruto, nome)
            if inicio > fim:
                raise ExpressaoCronInvalida(
                    f"faixa invertida em '{nome}': {corpo!r} (use {fim}-{inicio})"
                )
        else:
            inicio = fim = _inteiro(corpo, nome)

        if inicio < minimo or fim > maximo:
            raise ExpressaoCronInvalida(
                f"valor fora da faixa em '{nome}': {corpo!r} (esperado {minimo}-{maximo})"
            )

        valores.update(range(inicio, fim + 1, passo))

    if not valores:
        raise ExpressaoCronInvalida(f"campo '{nome}' não resultou em nenhum valor: {campo!r}")

    # O cron aceita 7 como domingo, além de 0. Normalizar evita que
    # `0 0 * * 7` nunca dispare.
    if nome == "dia_da_semana" and 7 in valores:
        valores.discard(7)
        valores.add(0)

    return frozenset(valores)


def _inteiro(bruto: str, nome: str) -> int:
    try:
        return int(bruto)
    except ValueError as erro:
        raise ExpressaoCronInvalida(f"valor não numérico em '{nome}': {bruto!r}") from erro


def valida(expressao: str) -> bool:
    try:
        interpretar(expressao)
        return True
    except ExpressaoCronInvalida:
        return False
