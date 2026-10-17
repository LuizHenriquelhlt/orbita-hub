"""Casos de uso do `catalog-service`."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from hub_catalog.ciclo_de_vida import (
    StatusAutomacao,
    requisitos_para_publicar,
    transicoes_de,
    validar_transicao,
)
from hub_catalog.esquemas import (
    AcaoDisponivel,
    AutomacaoCartao,
    AutomacaoDetalhe,
    ChecagemDeAcesso,
    GatilhoResumo,
    PedidoDeAtualizacao,
    PedidoDeCriacao,
    PedidoDeNovaVersao,
    PermissaoResumo,
    ResultadoDeImportacao,
    VersaoResumo,
)
from hub_catalog.manifesto import Manifesto, Recursos, carregar
from hub_catalog.modelos import (
    Automacao,
    AutomacaoPermissao,
    AutomacaoTag,
    AutomacaoVersao,
    Gatilho,
)
from hub_core import auditoria, events
from hub_core.auth.rbac import Papel, Permissao
from hub_core.auth.tokens import Identidade
from hub_core.errors import (
    Conflito,
    EntradaInvalida,
    NaoEncontrado,
    SemPermissao,
    TransicaoInvalida,
)
from hub_core.logging import obter_logger

log = obter_logger(__name__)

SERVICO = "catalog"

_NIVEIS_ORDENADOS = ("visualizar", "executar", "configurar")


# ─────────────────────────────────────────────────────────────────────────────
# Resolução de acesso (a camada ABAC)
# ─────────────────────────────────────────────────────────────────────────────


def nivel_de_acesso(automacao: Automacao, quem: Identidade) -> str:
    """O maior nível que esta pessoa tem nesta automação.

    Avalia as permissões declaradas da automação contra os atributos de quem
    pede, id, unidade e grupos, todos vindos dos claims do token. Nenhuma
    consulta a outro serviço.

    O administrador passa por cima da lista. É uma decisão deliberada e com
    contrapartida: simplifica a operação (não é preciso conceder acesso a si
    mesmo para investigar um problema) ao custo de o papel `admin` ser muito
    poderoso. O que compensa é a auditoria, toda ação de admin fica
    registrada, e `admin` é concedido a poucas pessoas.
    """
    if quem.e_admin:
        return "configurar"

    maior = "nenhum"
    for permissao in automacao.permissoes:
        if not _escopo_alcanca(permissao, quem):
            continue
        if _NIVEIS_ORDENADOS.index(permissao.nivel) > _indice_de_nivel(maior):
            maior = permissao.nivel
    return maior


def _indice_de_nivel(nivel: str) -> int:
    return _NIVEIS_ORDENADOS.index(nivel) if nivel in _NIVEIS_ORDENADOS else -1


def _escopo_alcanca(permissao: AutomacaoPermissao, quem: Identidade) -> bool:
    match permissao.escopo:
        case "todos":
            return True
        case "usuario":
            return permissao.escopo_id == quem.id
        case "unidade":
            return permissao.escopo_id is not None and permissao.escopo_id == quem.unidade_id
        case "equipe":
            return permissao.escopo_id in quem.grupos
        case _:
            return False


def pode_executar(automacao: Automacao, quem: Identidade) -> tuple[bool, str]:
    """Decide o disparo e explica o "não".

    A ordem das verificações segue a da mensagem mais útil: dizer "esta
    automação está arquivada" é mais informativo do que "você não tem acesso",
    mesmo quando as duas coisas são verdade.
    """
    situacao = automacao.situacao
    if not situacao.executavel:
        return False, f"automação {situacao.rotulo.lower()} não executa"

    if not quem.pode(Permissao.EXECUCAO_DISPARAR):
        return False, "seu papel não permite disparar execuções"

    nivel = nivel_de_acesso(automacao, quem)
    if _indice_de_nivel(nivel) < _NIVEIS_ORDENADOS.index("executar"):
        return False, "você não tem acesso de execução a esta automação"

    # Rascunho e homologação só rodam para quem é dono ou pode editar: é o
    # ensaio antes de publicar, e não deve aparecer como disponível para a
    # organização toda.
    if situacao in {StatusAutomacao.RASCUNHO, StatusAutomacao.HOMOLOGACAO}:
        proprietario = automacao.criado_por_id == quem.id
        if not (proprietario or quem.pode(Permissao.AUTOMACAO_EDITAR)):
            return False, f"automação em {situacao.rotulo.lower()}, ainda não publicada"

    return True, ""


# ─────────────────────────────────────────────────────────────────────────────
# Validação de parâmetros de entrada
# ─────────────────────────────────────────────────────────────────────────────


def validar_parametros(
    declarados: list[dict[str, Any]], recebidos: dict[str, Any]
) -> dict[str, Any]:
    """Confere os valores informados contra os parâmetros declarados.

    Chamado pelo `execution-service` antes de enfileirar. Fica aqui, e não lá,
    porque a declaração é do catálogo, e ter a validação junto da declaração
    é o que impede as duas de divergirem.

    Devolve o dicionário **saneado**: só as chaves declaradas, já convertidas
    para o tipo certo. Descartar o que não foi declarado importa: sem isso, um
    cliente pode injetar chaves arbitrárias que chegam intactas ao ambiente da
    automação.
    """
    por_nome = {str(p.get("nome")): p for p in declarados if p.get("nome")}
    problemas: list[dict[str, str]] = []
    saneado: dict[str, Any] = {}

    for nome, declaracao in por_nome.items():
        tipo = str(declaracao.get("tipo", "texto"))
        obrigatorio = bool(declaracao.get("obrigatorio"))
        rotulo = str(declaracao.get("rotulo", nome))
        bruto = recebidos.get(nome, declaracao.get("padrao"))

        vazio = bruto is None or (isinstance(bruto, str) and not bruto.strip())
        if vazio:
            if obrigatorio:
                problemas.append({"campo": nome, "problema": f"{rotulo} é obrigatório"})
            continue

        try:
            saneado[nome] = _converter(tipo, bruto, declaracao)
        except ValueError as erro:
            problemas.append({"campo": nome, "problema": f"{rotulo}: {erro}"})

    nao_declarados = sorted(set(recebidos) - set(por_nome))
    if problemas:
        raise EntradaInvalida("parâmetros inválidos", campos=problemas, ignorados=nao_declarados)

    if nao_declarados:
        # Não é erro, é ruído de um cliente desatualizado. Registrar permite
        # perceber que alguém está mandando um parâmetro que a automação
        # deixou de aceitar.
        log.info("catalogo.parametros_ignorados", chaves=nao_declarados)

    return saneado


def _converter(tipo: str, bruto: Any, declaracao: dict[str, Any]) -> Any:
    """Converte e valida um valor segundo o tipo declarado."""
    match tipo:
        case "texto" | "texto_longo":
            texto = str(bruto).strip()
            limite = 4000 if tipo == "texto_longo" else 500
            if len(texto) > limite:
                raise ValueError(f"máximo de {limite} caracteres")
            return texto

        case "numero_inteiro":
            try:
                valor = int(str(bruto).strip())
            except (TypeError, ValueError) as erro:
                raise ValueError("informe um número inteiro") from erro
            return _conferir_faixa(valor, declaracao)

        case "numero_decimal":
            try:
                # Aceita vírgula decimal: é o separador que o teclado em
                # português produz, e recusá-lo transforma a regra em
                # armadilha.
                valor = float(str(bruto).strip().replace(",", "."))
            except (TypeError, ValueError) as erro:
                raise ValueError("informe um número") from erro
            return _conferir_faixa(valor, declaracao)

        case "booleano":
            if isinstance(bruto, bool):
                return bruto
            return str(bruto).strip().lower() in {"true", "1", "sim", "on", "verdadeiro"}

        case "data":
            try:
                return date.fromisoformat(str(bruto).strip()).isoformat()
            except ValueError as erro:
                raise ValueError("use o formato AAAA-MM-DD") from erro

        case "mes":
            texto = str(bruto).strip()
            try:
                ano, mes = texto.split("-")
                if not (1 <= int(mes) <= 12) or not (1900 <= int(ano) <= 2200):
                    raise ValueError
            except (ValueError, AttributeError) as erro:
                raise ValueError("use o formato AAAA-MM") from erro
            return f"{int(ano):04d}-{int(mes):02d}"

        case "opcao":
            validas = {str(o.get("valor")) for o in declaracao.get("opcoes", [])}
            escolhido = str(bruto).strip()
            if escolhido not in validas:
                raise ValueError(f"opção inválida (válidas: {', '.join(sorted(validas))})")
            return escolhido

        case "multiopcao":
            validas = {str(o.get("valor")) for o in declaracao.get("opcoes", [])}
            escolhidos = bruto if isinstance(bruto, list) else [bruto]
            limpos = [str(e).strip() for e in escolhidos if str(e).strip()]
            invalidos = sorted(set(limpos) - validas)
            if invalidos:
                raise ValueError(f"opções inválidas: {', '.join(invalidos)}")
            return limpos

        case _:
            raise ValueError(f"tipo de parâmetro desconhecido: {tipo}")


def _conferir_faixa(valor: float, declaracao: dict[str, Any]) -> float | int:
    minimo = declaracao.get("minimo")
    maximo = declaracao.get("maximo")
    if minimo is not None and valor < float(minimo):
        raise ValueError(f"mínimo de {minimo}")
    if maximo is not None and valor > float(maximo):
        raise ValueError(f"máximo de {maximo}")
    return valor


# ─────────────────────────────────────────────────────────────────────────────
# Consultas
# ─────────────────────────────────────────────────────────────────────────────


def _consulta_base() -> Any:
    return select(Automacao).options(
        selectinload(Automacao.tags),
        selectinload(Automacao.permissoes),
        selectinload(Automacao.versoes),
    )


async def _obter_modelo(sessao: AsyncSession, slug: str) -> Automacao:
    consulta = (
        _consulta_base().options(selectinload(Automacao.gatilhos)).where(Automacao.slug == slug)
    )
    automacao = (await sessao.execute(consulta)).scalar_one_or_none()
    if automacao is None:
        raise NaoEncontrado("automação não encontrada", slug=slug)
    return automacao


def _para_cartao(automacao: Automacao, quem: Identidade) -> AutomacaoCartao:
    situacao = automacao.situacao
    permitido, _ = pode_executar(automacao, quem)
    versao = automacao.versao_atual
    return AutomacaoCartao(
        id=automacao.id,
        slug=automacao.slug,
        nome=automacao.nome,
        descricao=automacao.descricao,
        categoria=automacao.categoria,
        tipo=automacao.tipo,
        status=automacao.status,
        status_rotulo=situacao.rotulo,
        status_cor=situacao.cor,
        tags=sorted(t.tag for t in automacao.tags),
        modo_visual=automacao.modo_visual,
        versao_atual=versao.versao if versao else None,
        publicado_em=automacao.publicado_em,
        pode_executar=permitido,
    )


async def listar(
    sessao: AsyncSession,
    quem: Identidade,
    *,
    busca: str | None = None,
    categoria: str | None = None,
    tipo: str | None = None,
    tag: str | None = None,
    status: str | None = None,
    pagina: int = 1,
    por_pagina: int = 24,
) -> tuple[list[AutomacaoCartao], int, dict[str, dict[str, int]]]:
    """Lista o catálogo já filtrado pelo que a pessoa pode ver.

    A visibilidade é resolvida em duas etapas, e a divisão importa para o
    desempenho: o **status** entra na consulta SQL (é coluna indexada), e a
    permissão por escopo é aplicada em Python sobre a página devolvida. Levar a
    regra de escopo para o SQL exigiria um `OR` com subconsulta por tipo de
    escopo, que o planejador resolve mal; aplicá-la na página custa um laço
    sobre 24 itens.

    A contrapartida honesta: o `total` conta antes do filtro de escopo, então a
    contagem pode superestimar para quem tem acesso restrito. Em troca, a
    paginação não precisa carregar a tabela inteira para saber o tamanho. Para
    o uso do catálogo, onde a grande maioria das automações é de escopo
    `todos`, o desvio é pequeno e preferível a uma consulta que degrada.
    """
    consulta = _consulta_base()

    if quem.pode(Permissao.AUTOMACAO_EDITAR) or quem.e_admin:
        # Quem edita precisa ver rascunho e homologação para trabalhar nelas.
        if status:
            consulta = consulta.where(Automacao.status == status)
    else:
        visiveis = [StatusAutomacao.PUBLICADA.value, StatusAutomacao.DEPRECIADA.value]
        consulta = consulta.where(Automacao.status.in_(visiveis))
        if status and status in visiveis:
            consulta = consulta.where(Automacao.status == status)

    if categoria:
        consulta = consulta.where(Automacao.categoria == categoria)
    if tipo:
        consulta = consulta.where(Automacao.tipo == tipo)
    if tag:
        consulta = consulta.where(
            Automacao.id.in_(select(AutomacaoTag.automacao_id).where(AutomacaoTag.tag == tag))
        )
    if busca:
        termo = f"%{busca.strip()}%"
        consulta = consulta.where(
            or_(
                Automacao.nome.ilike(termo),
                Automacao.descricao.ilike(termo),
                Automacao.slug.ilike(termo),
            )
        )

    sem_ordem = consulta.order_by(None).subquery()
    total = (await sessao.execute(select(func.count()).select_from(sem_ordem))).scalar_one()

    facetas = await _calcular_facetas(sessao, consulta)

    pagina = max(1, pagina)
    por_pagina = min(max(1, por_pagina), 100)
    paginada = consulta.order_by(Automacao.nome).offset((pagina - 1) * por_pagina).limit(por_pagina)
    automacoes = (await sessao.execute(paginada)).scalars().all()

    itens = [
        _para_cartao(a, quem)
        for a in automacoes
        if quem.e_admin
        or quem.pode(Permissao.AUTOMACAO_EDITAR)
        or nivel_de_acesso(a, quem) != "nenhum"
    ]
    return itens, total, facetas


async def _calcular_facetas(sessao: AsyncSession, consulta: Any) -> dict[str, dict[str, int]]:
    """Contagem por categoria e por tipo, respeitando os filtros já aplicados.

    Duas agregações sobre a mesma subconsulta. Vêm junto com a página porque a
    interface mostra as duas coisas ao mesmo tempo, e separá-las em outro
    endpoint significaria repetir os mesmos filtros em uma segunda varredura.
    """
    base = consulta.order_by(None).subquery()

    facetas: dict[str, dict[str, int]] = {}
    for rotulo, coluna in (("categoria", base.c.categoria), ("tipo", base.c.tipo)):
        resultado = await sessao.execute(
            select(coluna, func.count()).group_by(coluna).order_by(coluna)
        )
        facetas[rotulo] = {str(valor): total for valor, total in resultado if valor}
    return facetas


async def obter(sessao: AsyncSession, slug: str, quem: Identidade) -> AutomacaoDetalhe:
    automacao = await _obter_modelo(sessao, slug)

    nivel = nivel_de_acesso(automacao, quem)
    pode_editar = quem.pode(Permissao.AUTOMACAO_EDITAR) or quem.e_admin
    if nivel == "nenhum" and not pode_editar:
        # 404 e não 403: dizer "existe mas você não pode ver" revela a
        # existência da automação, que já é informação sobre a organização.
        raise NaoEncontrado("automação não encontrada", slug=slug)

    # Rascunho e homologação só aparecem para quem edita ou para quem criou,
    # é o ensaio antes de publicar, e não deve constar do catálogo da
    # organização. Também 404, pelo mesmo motivo acima.
    if (
        not automacao.situacao.visivel_no_catalogo
        and not pode_editar
        and automacao.criado_por_id != quem.id
    ):
        raise NaoEncontrado("automação não encontrada", slug=slug)

    cartao = _para_cartao(automacao, quem)
    pendentes = requisitos_para_publicar(automacao)

    acoes = [
        AcaoDisponivel(para=t.para.value, acao=t.acao, permissao=t.permissao)
        for t in transicoes_de(automacao.situacao)
        if quem.pode(t.permissao)
        # Publicar só aparece se os requisitos estiverem cumpridos: um botão
        # que sempre devolve erro é pior do que botão ausente com a lista do
        # que falta ao lado.
        and not (t.para is StatusAutomacao.PUBLICADA and pendentes)
    ]

    return AutomacaoDetalhe(
        **cartao.model_dump(),
        readme=automacao.readme,
        responsavel_tecnico=automacao.responsavel_tecnico,
        responsavel_negocio=automacao.responsavel_negocio,
        unidade_dona_id=automacao.unidade_dona_id,
        imagem=automacao.imagem,
        entrypoint=list(automacao.entrypoint or []),
        rede_habilitada=automacao.rede_habilitada,
        max_execucoes_concorrentes=automacao.max_execucoes_concorrentes,
        limites=Recursos.model_validate(automacao.limites or {}),
        consumo_estimado=automacao.consumo_estimado or {},
        parametros=automacao.parametros or [],
        variaveis_ambiente=automacao.variaveis_ambiente or [],
        versoes=[VersaoResumo.model_validate(v) for v in automacao.versoes],
        gatilhos=[GatilhoResumo.model_validate(g) for g in automacao.gatilhos],
        permissoes=[PermissaoResumo.model_validate(p) for p in automacao.permissoes],
        acoes_disponiveis=acoes,
        requisitos_pendentes=pendentes,
        criado_em=automacao.criado_em,
        atualizado_em=automacao.atualizado_em,
    )


async def preparar_execucao(
    sessao: AsyncSession,
    slug: str,
    quem: Identidade,
    *,
    parametros: dict[str, Any] | None = None,
    fila: str | None = None,
) -> ChecagemDeAcesso:
    """Responde ao `execution-service`: pode disparar, e com quais parâmetros?

    Devolve, junto com a decisão, tudo que o motor precisa para montar o pedido
    de execução, imagem, entrypoint, limites, roteiro, e os parâmetros já
    validados e convertidos.

    A ordem importa: o acesso é verificado **antes** da validação dos
    parâmetros. Validar primeiro responderia, pela mensagem de erro, qual é a
    forma dos parâmetros de uma automação a que a pessoa não tem acesso.
    """
    automacao = await _obter_modelo(sessao, slug)
    permitido, motivo = pode_executar(automacao, quem)
    nivel = nivel_de_acesso(automacao, quem)
    versao = automacao.versao_atual

    saneados: dict[str, Any] = {}
    if permitido:
        saneados = validar_parametros(automacao.parametros or [], parametros or {})

    return ChecagemDeAcesso(
        parametros_saneados=saneados,
        nome=automacao.nome,
        slug=automacao.slug,
        permitido=permitido,
        motivo=motivo,
        nivel=nivel,  # type: ignore[arg-type]
        automacao_id=automacao.id,
        versao_id=versao.id if versao else None,
        versao=versao.versao if versao else None,
        imagem=(versao.tag_imagem if versao and versao.tag_imagem else automacao.imagem),
        entrypoint=list(automacao.entrypoint or []),
        limites=Recursos.model_validate(automacao.limites or {}),
        rede_habilitada=automacao.rede_habilitada,
        max_execucoes_concorrentes=automacao.max_execucoes_concorrentes,
        parametros=automacao.parametros or [],
        variaveis_ambiente=automacao.variaveis_ambiente or [],
        roteiro_simulado=automacao.roteiro_simulado or [],
        fila_sugerida=fila or _fila_para(automacao),
    )


def _fila_para(automacao: Automacao) -> str:
    """Escolhe a fila conforme o perfil da automação.

    Automação com duração estimada longa vai para a fila de lote, para não
    ficar à frente de uma execução curta que alguém está esperando na tela. É
    uma heurística simples; o gatilho pode sobrescrevê-la.
    """
    estimada = (automacao.consumo_estimado or {}).get("duracao_media_segundos")
    if isinstance(estimada, int | float) and estimada > 180:
        return "execucoes.lote"
    if automacao.modo_visual:
        return "execucoes.interativa"
    return "execucoes.padrao"


# ─────────────────────────────────────────────────────────────────────────────
# Escrita
# ─────────────────────────────────────────────────────────────────────────────


async def criar(
    sessao: AsyncSession,
    pedido: PedidoDeCriacao,
    *,
    autor: Identidade,
    requisicao: Any = None,
) -> AutomacaoDetalhe:
    automacao = Automacao(
        slug=pedido.slug,
        nome=pedido.nome.strip(),
        descricao=pedido.descricao.strip(),
        categoria=pedido.categoria,
        tipo=pedido.tipo,
        status=StatusAutomacao.RASCUNHO.value,
        responsavel_tecnico=pedido.responsavel_tecnico,
        responsavel_negocio=pedido.responsavel_negocio,
        unidade_dona_id=pedido.unidade_dona_id or autor.unidade_id,
        criado_por_id=autor.id,
        imagem=pedido.imagem,
        entrypoint=pedido.entrypoint,
        readme=pedido.readme,
        modo_visual=pedido.modo_visual,
        rede_habilitada=pedido.rede_habilitada,
        max_execucoes_concorrentes=pedido.max_execucoes_concorrentes,
        limites=pedido.limites.model_dump(),
        parametros=[p.model_dump(mode="json") for p in pedido.parametros],
        variaveis_ambiente=[v.model_dump(mode="json") for v in pedido.variaveis_ambiente],
    )
    sessao.add(automacao)

    try:
        await sessao.flush()
    except IntegrityError as erro:
        await sessao.rollback()
        raise Conflito(
            f"já existe uma automação com o identificador '{pedido.slug}'", slug=pedido.slug
        ) from erro

    for tag in dict.fromkeys(t.strip().lower() for t in pedido.tags if t.strip()):
        sessao.add(AutomacaoTag(automacao_id=automacao.id, tag=tag))

    for gatilho in pedido.gatilhos or []:
        sessao.add(
            Gatilho(
                automacao_id=automacao.id,
                tipo=gatilho.tipo,
                expressao=gatilho.expressao,
                fila=gatilho.fila,
                parametros_padrao=gatilho.parametros_padrao,
            )
        )
    if not pedido.gatilhos:
        # Toda automação tem de poder ser disparada de algum jeito; sem isto,
        # uma automação criada pela interface nasce sem nenhum gatilho e não
        # roda, sem explicação.
        sessao.add(Gatilho(automacao_id=automacao.id, tipo="manual"))

    # Quem cria tem acesso de configuração desde o rascunho, senão não
    # consegue testar a própria automação antes de publicá-la.
    sessao.add(
        AutomacaoPermissao(
            automacao_id=automacao.id, escopo="usuario", escopo_id=autor.id, nivel="configurar"
        )
    )

    await auditoria.registrar(
        sessao,
        acao=events.AUTOMACAO_CRIADA,
        recurso=pedido.slug,
        ator=autor,
        servico=SERVICO,
        requisicao=requisicao,
        detalhes={"nome": pedido.nome, "categoria": pedido.categoria, "tipo": pedido.tipo},
    )
    await sessao.commit()
    return await obter(sessao, pedido.slug, autor)


async def atualizar(
    sessao: AsyncSession,
    slug: str,
    pedido: PedidoDeAtualizacao,
    *,
    autor: Identidade,
    requisicao: Any = None,
) -> AutomacaoDetalhe:
    automacao = await _obter_modelo(sessao, slug)

    if automacao.situacao is StatusAutomacao.ARQUIVADA:
        raise TransicaoInvalida(
            "automação arquivada não pode ser editada",
            dica="desarquivar não é previsto; crie uma nova automação",
        )

    alteracoes = pedido.alteracoes()
    tags = alteracoes.pop("tags", None)

    for campo, valor in alteracoes.items():
        setattr(automacao, campo, valor)

    if tags is not None:
        await sessao.execute(delete(AutomacaoTag).where(AutomacaoTag.automacao_id == automacao.id))
        for tag in dict.fromkeys(t.strip().lower() for t in tags if t.strip()):
            sessao.add(AutomacaoTag(automacao_id=automacao.id, tag=tag))

    # Editar algo já publicado devolve para homologação: a aprovação valeu
    # para o conteúdo aprovado, não para o que vier depois dele.
    voltou_para_homologacao = False
    if automacao.situacao is StatusAutomacao.PUBLICADA and alteracoes:
        automacao.status = StatusAutomacao.HOMOLOGACAO.value
        voltou_para_homologacao = True

    await auditoria.registrar(
        sessao,
        acao="automacao.editada",
        recurso=slug,
        ator=autor,
        servico=SERVICO,
        requisicao=requisicao,
        detalhes={
            "campos": sorted(alteracoes),
            "tags_alteradas": tags is not None,
            "voltou_para_homologacao": voltou_para_homologacao,
        },
    )
    await sessao.commit()
    return await obter(sessao, slug, autor)


async def transicionar(
    sessao: AsyncSession,
    slug: str,
    para: StatusAutomacao,
    *,
    autor: Identidade,
    justificativa: str | None = None,
    requisicao: Any = None,
) -> AutomacaoDetalhe:
    automacao = await _obter_modelo(sessao, slug)
    de = automacao.situacao

    transicao = validar_transicao(de, para)

    if not autor.pode(transicao.permissao):
        raise SemPermissao(
            f"'{transicao.acao}' exige a permissão {transicao.permissao}",
            papeis=list(autor.papeis),
        )

    if para is StatusAutomacao.PUBLICADA:
        pendentes = requisitos_para_publicar(automacao)
        if pendentes:
            raise TransicaoInvalida(
                "a automação ainda não atende aos requisitos de publicação",
                faltando=pendentes,
            )
        automacao.publicado_em = datetime.now(UTC).date()
        automacao.depreciado_em = None

    if para is StatusAutomacao.DEPRECIADA:
        automacao.depreciado_em = datetime.now(UTC).date()

    automacao.status = para.value

    await auditoria.registrar(
        sessao,
        acao=events.AUTOMACAO_STATUS_ALTERADO,
        recurso=slug,
        ator=autor,
        servico=SERVICO,
        requisicao=requisicao,
        detalhes={"de": de.value, "para": para.value, "justificativa": justificativa},
    )
    await sessao.commit()

    await events.barramento.publicar(
        events.EventoDeDominio(
            nome=events.AUTOMACAO_STATUS_ALTERADO,
            recurso=slug,
            dados={"de": de.value, "para": para.value},
            ator_id=autor.id,
            ator_nome=autor.nome,
        )
    )
    return await obter(sessao, slug, autor)


async def registrar_versao(
    sessao: AsyncSession,
    slug: str,
    pedido: PedidoDeNovaVersao,
    *,
    autor: Identidade,
    manifesto: dict[str, Any] | None = None,
    requisicao: Any = None,
) -> AutomacaoDetalhe:
    automacao = await _obter_modelo(sessao, slug)

    sessao.add(
        AutomacaoVersao(
            automacao_id=automacao.id,
            versao=pedido.versao,
            tag_imagem=pedido.tag_imagem or automacao.imagem,
            changelog=pedido.changelog,
            manifesto=manifesto or {},
            criado_por_id=autor.id,
        )
    )

    try:
        await sessao.flush()
    except IntegrityError as erro:
        await sessao.rollback()
        raise Conflito(
            f"a versão {pedido.versao} já existe nesta automação",
            slug=slug,
            versao=pedido.versao,
        ) from erro

    await auditoria.registrar(
        sessao,
        acao=events.AUTOMACAO_VERSAO_PUBLICADA,
        recurso=slug,
        ator=autor,
        servico=SERVICO,
        requisicao=requisicao,
        detalhes={"versao": pedido.versao},
    )
    await sessao.commit()
    return await obter(sessao, slug, autor)


async def importar_manifesto(
    sessao: AsyncSession,
    manifesto_yaml: str,
    *,
    autor: Identidade,
    publicar: bool = False,
    resolver_unidade: Any = None,
    requisicao: Any = None,
) -> ResultadoDeImportacao:
    """Cria ou atualiza uma automação a partir de um `automation.yaml`.

    É o caminho que torna o catálogo extensível sem deploy: o time da
    automação versiona o manifesto junto do código dela e o submete. Idempotente
    por `slug`, reimportar atualiza em vez de duplicar, que é o que permite o
    pipeline da automação rodar a importação a cada release.
    """
    manifesto = carregar(manifesto_yaml)
    avisos: list[str] = []

    existente = (
        await sessao.execute(select(Automacao).where(Automacao.slug == manifesto.slug))
    ).scalar_one_or_none()

    unidade_id: uuid.UUID | None = None
    if manifesto.metadata.unidade and resolver_unidade is not None:
        unidade_id = await resolver_unidade(manifesto.metadata.unidade)
        if unidade_id is None:
            avisos.append(
                f"unidade '{manifesto.metadata.unidade}' não encontrada, "
                "a automação ficou sem unidade dona"
            )

    criada = existente is None
    automacao = existente or Automacao(slug=manifesto.slug, criado_por_id=autor.id)
    _aplicar_manifesto(automacao, manifesto, unidade_id=unidade_id)

    if criada:
        sessao.add(automacao)
        await sessao.flush()
        sessao.add(
            AutomacaoPermissao(
                automacao_id=automacao.id,
                escopo="usuario",
                escopo_id=autor.id,
                nivel="configurar",
            )
        )

    await _sincronizar_tags(sessao, automacao, manifesto)
    await _sincronizar_gatilhos(sessao, automacao, manifesto)
    await _sincronizar_permissoes(sessao, automacao, manifesto, avisos, resolver_unidade)

    # A versão é registrada se ainda não existir. Reimportar o mesmo manifesto
    # não cria versão duplicada nem falha, apenas atualiza os campos.
    ja_tem_versao = (
        await sessao.execute(
            select(AutomacaoVersao.id).where(
                AutomacaoVersao.automacao_id == automacao.id,
                AutomacaoVersao.versao == manifesto.metadata.versao,
            )
        )
    ).first()
    if ja_tem_versao is None:
        sessao.add(
            AutomacaoVersao(
                automacao_id=automacao.id,
                versao=manifesto.metadata.versao,
                tag_imagem=manifesto.spec.imagem,
                changelog=manifesto.metadata.changelog,
                manifesto=manifesto.model_dump(mode="json"),
                criado_por_id=autor.id,
            )
        )
    else:
        avisos.append(f"a versão {manifesto.metadata.versao} já estava registrada")

    await sessao.flush()

    if publicar:
        if not autor.pode(Permissao.AUTOMACAO_PUBLICAR):
            avisos.append("sem permissão para publicar, a automação ficou em rascunho")
        else:
            await sessao.refresh(automacao, ["versoes"])
            pendentes = requisitos_para_publicar(automacao)
            if pendentes:
                avisos.append(f"não publicada; falta: {', '.join(pendentes)}")
            else:
                automacao.status = StatusAutomacao.PUBLICADA.value
                automacao.publicado_em = datetime.now(UTC).date()

    await auditoria.registrar(
        sessao,
        acao="automacao.importada",
        recurso=manifesto.slug,
        ator=autor,
        servico=SERVICO,
        requisicao=requisicao,
        detalhes={
            "versao": manifesto.metadata.versao,
            "criada": criada,
            "avisos": avisos,
        },
    )
    await sessao.commit()

    return ResultadoDeImportacao(
        slug=manifesto.slug,
        criada=criada,
        versao=manifesto.metadata.versao,
        status=automacao.status,
        avisos=avisos,
    )


def _aplicar_manifesto(
    automacao: Automacao, manifesto: Manifesto, *, unidade_id: uuid.UUID | None
) -> None:
    meta, spec = manifesto.metadata, manifesto.spec

    automacao.nome = meta.titulo
    automacao.descricao = meta.descricao
    automacao.categoria = meta.categoria
    automacao.tipo = spec.tipo
    automacao.responsavel_tecnico = meta.responsavel_tecnico
    automacao.responsavel_negocio = meta.responsavel_negocio
    if unidade_id is not None:
        automacao.unidade_dona_id = unidade_id

    automacao.imagem = spec.imagem
    automacao.entrypoint = spec.entrypoint
    automacao.modo_visual = spec.modo_visual
    automacao.rede_habilitada = spec.rede_habilitada
    automacao.max_execucoes_concorrentes = spec.max_execucoes_concorrentes
    automacao.limites = spec.recursos.model_dump()
    automacao.consumo_estimado = spec.consumo_estimado
    automacao.parametros = [p.model_dump(mode="json") for p in spec.parametros]
    automacao.variaveis_ambiente = [v.model_dump(mode="json") for v in spec.variaveis_ambiente]
    automacao.roteiro_simulado = spec.roteiro_simulado
    if spec.readme:
        automacao.readme = spec.readme
    if not automacao.status:
        automacao.status = StatusAutomacao.RASCUNHO.value


async def _sincronizar_tags(
    sessao: AsyncSession, automacao: Automacao, manifesto: Manifesto
) -> None:
    await sessao.execute(delete(AutomacaoTag).where(AutomacaoTag.automacao_id == automacao.id))
    for tag in dict.fromkeys(manifesto.metadata.tags):
        sessao.add(AutomacaoTag(automacao_id=automacao.id, tag=tag))


async def _sincronizar_gatilhos(
    sessao: AsyncSession, automacao: Automacao, manifesto: Manifesto
) -> None:
    """Substitui os gatilhos pelos declarados no manifesto.

    Substituição total, e não merge: o manifesto é a declaração completa do
    estado desejado. Um gatilho removido do arquivo tem de desaparecer, senão
    um agendamento continua disparando depois de ter sido retirado do
    manifesto, e ninguém acha o motivo.
    """
    await sessao.execute(delete(Gatilho).where(Gatilho.automacao_id == automacao.id))
    declarados = manifesto.spec.gatilhos or []
    for gatilho in declarados:
        sessao.add(
            Gatilho(
                automacao_id=automacao.id,
                tipo=gatilho.tipo,
                expressao=gatilho.expressao,
                fila=gatilho.fila,
                parametros_padrao=gatilho.parametros_padrao,
            )
        )
    if not declarados:
        sessao.add(Gatilho(automacao_id=automacao.id, tipo="manual"))


async def _sincronizar_permissoes(
    sessao: AsyncSession,
    automacao: Automacao,
    manifesto: Manifesto,
    avisos: list[str],
    resolver_unidade: Any,
) -> None:
    """Aplica as permissões declaradas, preservando as concedidas na interface.

    Só as de escopo `todos` e `unidade` são substituídas, as que o manifesto
    sabe declarar. Concessões pontuais a pessoa ou equipe, feitas na tela de
    acesso, sobrevivem à reimportação: elas são decisão operacional, e um
    `git push` na automação não deve revogá-las.
    """
    await sessao.execute(
        delete(AutomacaoPermissao).where(
            AutomacaoPermissao.automacao_id == automacao.id,
            AutomacaoPermissao.escopo.in_(["todos", "unidade"]),
        )
    )

    for permissao in manifesto.spec.permissoes:
        escopo_id: uuid.UUID | None = None
        if permissao.escopo == "unidade":
            if not permissao.identificador or resolver_unidade is None:
                avisos.append("permissão de escopo 'unidade' sem identificador, ignorada")
                continue
            escopo_id = await resolver_unidade(permissao.identificador)
            if escopo_id is None:
                avisos.append(
                    f"unidade '{permissao.identificador}' não encontrada, permissão ignorada"
                )
                continue
        elif permissao.escopo != "todos":
            # `usuario` e `equipe` não vêm do manifesto: dependem de ids que só
            # existem no ambiente, e manifesto com UUID cravado não é portável.
            avisos.append(
                f"permissão de escopo '{permissao.escopo}' deve ser concedida pela "
                "interface, não pelo manifesto, ignorada"
            )
            continue

        sessao.add(
            AutomacaoPermissao(
                automacao_id=automacao.id,
                escopo=permissao.escopo,
                escopo_id=escopo_id,
                nivel=permissao.nivel,
            )
        )


async def conceder_permissao(
    sessao: AsyncSession,
    slug: str,
    *,
    escopo: str,
    escopo_id: uuid.UUID | None,
    nivel: str,
    autor: Identidade,
    requisicao: Any = None,
) -> AutomacaoDetalhe:
    automacao = await _obter_modelo(sessao, slug)

    ja_existe = any(
        p.escopo == escopo and p.escopo_id == escopo_id and p.nivel == nivel
        for p in automacao.permissoes
    )
    if ja_existe:
        return await obter(sessao, slug, autor)

    sessao.add(
        AutomacaoPermissao(
            automacao_id=automacao.id, escopo=escopo, escopo_id=escopo_id, nivel=nivel
        )
    )
    await auditoria.registrar(
        sessao,
        acao=events.ACESSO_CONCEDIDO,
        recurso=slug,
        ator=autor,
        servico=SERVICO,
        requisicao=requisicao,
        detalhes={"escopo": escopo, "escopo_id": str(escopo_id), "nivel": nivel},
    )
    await sessao.commit()
    return await obter(sessao, slug, autor)


async def listar_gatilhos_cron(sessao: AsyncSession) -> list[dict[str, Any]]:
    """Agendamentos ativos de automações executáveis.

    Consumido pelo agendador do `execution-service`. Devolve dicionário simples
    em vez de modelo para que o motor não precise importar nada do catálogo,
    a fronteira entre os dois é o contrato HTTP, não a classe.
    """
    consulta = (
        select(Gatilho, Automacao)
        .join(Automacao, Automacao.id == Gatilho.automacao_id)
        .where(
            Gatilho.tipo == "cron",
            Gatilho.ativo.is_(True),
            Automacao.status.in_(
                [StatusAutomacao.PUBLICADA.value, StatusAutomacao.DEPRECIADA.value]
            ),
        )
        .order_by(Automacao.slug)
    )
    linhas = (await sessao.execute(consulta)).all()
    return [
        {
            "gatilho_id": str(gatilho.id),
            "slug": automacao.slug,
            "expressao": gatilho.expressao,
            "fila": gatilho.fila,
            "parametros_padrao": gatilho.parametros_padrao,
            "unidade_dona_id": str(automacao.unidade_dona_id)
            if automacao.unidade_dona_id
            else None,
        }
        for gatilho, automacao in linhas
    ]


def papel_minimo_para_publicar() -> Papel:
    return Papel.GESTOR
