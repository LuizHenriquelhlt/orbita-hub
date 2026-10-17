"""Filtros e tags de template.

Tudo aqui é formatação para leitura humana. A regra que define o que entra: se
é uma decisão de apresentação que apareceria em três templates diferentes,
vira filtro; se é regra de negócio, fica no serviço.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from django import template
from django.utils import timezone
from django.utils.safestring import mark_safe

# O nome da variável é obrigatório: o Django procura literalmente por
# `register` no módulo (`django.template.backends.django.import_library`).
# Chamá-la de `registrador`, como o resto do projeto sugeriria, faz a
# biblioteca simplesmente não ser registrada, e o erro que aparece é
# "'orbita' is not a registered tag library", que não diz o motivo.
register = template.Library()

# Classe CSS por status de execução. Mapa aqui, e não `{% if %}` encadeado no
# template, para que acrescentar um status seja uma linha.
CORES_DE_STATUS = {
    "enfileirada": "neutro",
    "executando": "ativo",
    "sucesso": "sucesso",
    "falha": "erro",
    "cancelada": "inativo",
    "timeout": "aviso",
}

ROTULOS_DE_STATUS = {
    "enfileirada": "Na fila",
    "executando": "Executando",
    "sucesso": "Sucesso",
    "falha": "Falha",
    "cancelada": "Cancelada",
    "timeout": "Tempo esgotado",
}

CORES_DE_NIVEL = {
    "DEBUG": "debug",
    "INFO": "info",
    "WARNING": "aviso",
    "ERROR": "erro",
}

ROTULOS_DE_CATEGORIA = {
    "relatorios": "Relatórios",
    "cadastro": "Cadastro",
    "fiscalizacao": "Fiscalização",
    "integracao": "Integração",
    "monitoramento": "Monitoramento",
    "documentos": "Documentos",
}

ROTULOS_DE_TIPO = {
    "batch": "Lote",
    "web": "Web",
    "api": "API",
    "ia": "IA",
    "crawler": "Coletor",
    "rpa": "RPA",
    "workflow": "Fluxo",
}


@register.filter
def cor_de_status(status: str) -> str:
    return CORES_DE_STATUS.get(str(status), "neutro")


@register.filter
def rotulo_de_status(status: str) -> str:
    return ROTULOS_DE_STATUS.get(str(status), str(status).capitalize())


@register.filter
def cor_de_nivel(nivel: str) -> str:
    return CORES_DE_NIVEL.get(str(nivel).upper(), "info")


@register.filter
def rotulo_de_categoria(categoria: str) -> str:
    return ROTULOS_DE_CATEGORIA.get(str(categoria), str(categoria).capitalize())


@register.filter
def rotulo_de_tipo(tipo: str) -> str:
    return ROTULOS_DE_TIPO.get(str(tipo), str(tipo).upper())


@register.filter
def duracao(milissegundos: Any) -> str:
    """`6044` -> `6,0 s`; `95000` -> `1min 35s`.

    Troca de unidade conforme a magnitude. Mostrar "6044 ms" obriga quem lê a
    dividir por mil de cabeça; mostrar "0,1 min" perde a precisão que importa
    em execução curta.
    """
    if milissegundos in (None, ""):
        return "-"
    try:
        ms = float(milissegundos)
    except (TypeError, ValueError):
        return "-"

    if ms < 1000:
        return f"{ms:.0f} ms"
    segundos = ms / 1000
    if segundos < 60:
        return f"{segundos:.1f} s".replace(".", ",")
    minutos, resto = divmod(int(segundos), 60)
    if minutos < 60:
        return f"{minutos}min {resto}s"
    horas, minutos = divmod(minutos, 60)
    return f"{horas}h {minutos}min"


@register.filter
def tamanho_de_arquivo(bytes_: Any) -> str:
    if bytes_ in (None, ""):
        return "-"
    try:
        tamanho = float(bytes_)
    except (TypeError, ValueError):
        return "-"
    for unidade in ("B", "KB", "MB", "GB"):
        if tamanho < 1024 or unidade == "GB":
            if unidade == "B":
                return f"{tamanho:.0f} B"
            return f"{tamanho:.1f} {unidade}".replace(".", ",")
        tamanho /= 1024
    return f"{tamanho:.1f} GB"


@register.filter
def quando(valor: Any) -> str:
    """Data relativa para o passado recente, absoluta para o resto.

    "há 3 min" é mais útil do que "17/10/2026 10:58" quando acabou de
    acontecer, e inútil quando foi em março, "há 7 meses" obriga a pessoa a
    calcular a data. O corte fica em uma semana.
    """
    momento = _como_data(valor)
    if momento is None:
        return "-"

    agora = timezone.now()
    delta = agora - momento
    segundos = delta.total_seconds()

    if segundos < 0:
        # Futuro: é o caso de `proxima_em` de um agendamento.
        futuro = -segundos
        if futuro < 60:
            return "em instantes"
        if futuro < 3600:
            return f"em {int(futuro // 60)} min"
        if futuro < 86400:
            return f"em {int(futuro // 3600)} h"
        return timezone.localtime(momento).strftime("%d/%m às %H:%M")

    if segundos < 10:
        return "agora"
    if segundos < 60:
        return f"há {int(segundos)} s"
    if segundos < 3600:
        return f"há {int(segundos // 60)} min"
    if segundos < 86400:
        return f"há {int(segundos // 3600)} h"
    if segundos < 604800:
        dias = int(segundos // 86400)
        return f"há {dias} dia{'s' if dias > 1 else ''}"
    return timezone.localtime(momento).strftime("%d/%m/%Y")


@register.filter
def data_e_hora(valor: Any) -> str:
    momento = _como_data(valor)
    if momento is None:
        return "-"
    return timezone.localtime(momento).strftime("%d/%m/%Y às %H:%M:%S")


@register.filter
def numero(valor: Any, decimais: int = 0) -> str:
    """Formato brasileiro: ponto como separador de milhar, vírgula no decimal."""
    if valor in (None, ""):
        return "-"
    try:
        convertido = float(valor)
    except (TypeError, ValueError):
        return str(valor)
    formatado = f"{convertido:,.{decimais}f}"
    return formatado.replace(",", " ").replace(".", ",").replace(" ", ".")


@register.filter
def porcentagem(valor: Any, decimais: int = 1) -> str:
    if valor in (None, ""):
        return "-"
    try:
        return f"{float(valor):.{decimais}f}%".replace(".", ",")
    except (TypeError, ValueError):
        return "-"


@register.filter
def chave(mapa: Any, nome: Any) -> Any:
    """Acesso a dicionário por chave variável.

    Os templates do Django resolvem `{{ mapa.chave }}` pelo nome literal, então
    não há como usar uma variável como chave. Este filtro é a saída padrão
    para isso, e é usado nas facetas do catálogo, onde as chaves vêm do dado.
    """
    if mapa is None:
        return None
    try:
        return mapa.get(nome)
    except AttributeError:
        return None


@register.filter
def barra(valor: Any, maximo: Any) -> str:
    """Largura em porcentagem, para as barras do gráfico do painel.

    O gráfico é desenhado com `div`s e CSS, sem biblioteca de JavaScript. Para
    uma série de 14 pontos, carregar um pacote de gráficos custaria mais do que
    o gráfico inteiro, e o resultado é acessível e imprime bem.
    """
    try:
        v, m = float(valor), float(maximo)
    except (TypeError, ValueError):
        return "0"
    if m <= 0:
        return "0"
    return f"{min(100.0, 100.0 * v / m):.1f}"


@register.simple_tag
def etiqueta(texto: str, cor: str = "neutro") -> str:
    from django.utils.html import escape

    return mark_safe(f'<span class="etiqueta etiqueta--{escape(cor)}">{escape(texto)}</span>')


@register.simple_tag(takes_context=True)
def consulta_com(context: dict[str, Any], **novos: Any) -> str:
    """Reconstrói a query string trocando apenas os parâmetros informados.

        <a href="?{% consulta_com pagina=3 %}">

    Sem isto, cada link de paginação teria de reescrever todos os filtros
    ativos à mão, e esquecer um significa perder o filtro ao virar a página,
    que é o bug mais irritante de uma listagem.
    """
    request = context.get("request")
    if request is None:
        return ""
    parametros = request.GET.copy()
    for nome, valor in novos.items():
        if valor in (None, ""):
            parametros.pop(nome, None)
        else:
            parametros[nome] = valor
    return parametros.urlencode()


def _como_data(valor: Any) -> datetime | None:
    if valor in (None, ""):
        return None
    if isinstance(valor, datetime):
        momento = valor
    else:
        try:
            # `fromisoformat` do Python 3.11+ aceita o `Z` final que o FastAPI
            # emite; antes disso era preciso substituir por `+00:00`.
            momento = datetime.fromisoformat(str(valor))
        except ValueError:
            return None
    if timezone.is_naive(momento):
        momento = timezone.make_aware(momento, timezone.get_default_timezone())
    return momento
