"""Casos de uso do `identity-service`.

As rotas (`rotas.py`) só traduzem HTTP; a regra está aqui. O ganho prático é
que estas funções recebem uma `AsyncSession` e valores simples, então o teste
delas não precisa de cliente HTTP, de token nem de aplicação de pé.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from hub_core import auditoria
from hub_core.auth import gerar_hash, precisa_rehash, verificar
from hub_core.auth.rbac import Papel
from hub_core.auth.tokens import Identidade
from hub_core.errors import Conflito, NaoAutenticado, NaoEncontrado, SemPermissao
from hub_core.logging import obter_logger
from hub_identity import cpf as cpf_util
from hub_identity.esquemas import (
    GrupoResumo,
    PedidoDeCriacaoDeUsuario,
    UnidadeResumo,
    UsuarioDetalhe,
    UsuarioResumo,
)
from hub_identity.modelos import (
    Grupo,
    PapelGrupo,
    PapelUsuario,
    Unidade,
    Usuario,
    UsuarioGrupo,
)

log = obter_logger(__name__)

SERVICO = "identity"

MAX_TENTATIVAS = 5
DURACAO_DO_BLOQUEIO = timedelta(minutes=15)

# Hash de uma senha que não existe, usado para igualar o tempo de resposta
# quando o CPF não está cadastrado. Ver `autenticar`.
_HASH_FALSO = gerar_hash("senha-que-nao-existe-apenas-para-gastar-tempo")


async def papeis_efetivos(sessao: AsyncSession, usuario_id: uuid.UUID) -> tuple[str, ...]:
    """Papéis diretos ∪ papéis herdados dos grupos.

    Uma consulta com `UNION` em vez de duas e um `set` em Python: além de ser
    um round-trip menos, deixa a deduplicação no banco, que é onde ela custa
    menos.
    """
    diretos = select(PapelUsuario.papel).where(PapelUsuario.usuario_id == usuario_id)
    por_grupo = (
        select(PapelGrupo.papel)
        .join(UsuarioGrupo, UsuarioGrupo.grupo_id == PapelGrupo.grupo_id)
        .where(UsuarioGrupo.usuario_id == usuario_id)
    )
    resultado = await sessao.execute(diretos.union(por_grupo))
    return tuple(sorted(linha[0] for linha in resultado))


async def grupos_de(sessao: AsyncSession, usuario_id: uuid.UUID) -> tuple[uuid.UUID, ...]:
    """Ids dos grupos da pessoa. Vão nos claims do token.

    É o que permite ao `catalog-service` resolver permissão de escopo `equipe`
    sem consultar este serviço, ver `hub_core.auth.tokens.CLAIM_GRUPOS`.
    """
    resultado = await sessao.execute(
        select(UsuarioGrupo.grupo_id).where(UsuarioGrupo.usuario_id == usuario_id)
    )
    return tuple(resultado.scalars().all())


async def _montar_identidade(sessao: AsyncSession, usuario: Usuario) -> Identidade:
    papeis = await papeis_efetivos(sessao, usuario.id)
    grupos = await grupos_de(sessao, usuario.id)
    return Identidade(
        id=usuario.id,
        nome=usuario.nome,
        email=usuario.email,
        cpf=usuario.cpf,
        papeis=papeis,
        unidade_id=usuario.unidade_id,
        unidade_sigla=usuario.unidade.sigla if usuario.unidade else "",
        grupos=grupos,
    )


async def autenticar(
    sessao: AsyncSession,
    *,
    cpf: str,
    senha: str,
    requisicao: Any = None,
) -> Identidade:
    """Valida credencial e devolve a identidade. Levanta `NaoAutenticado`.

    ## Três decisões de segurança

    **Mensagem única.** "CPF ou senha inválidos", sempre, nunca "este CPF não
    existe". A segunda forma transforma a tela de login em um verificador de
    cadastro, e, em um sistema cuja chave é CPF, isso é consulta de base de
    pessoas de graça.

    **Tempo constante.** A mensagem igual não basta se a resposta para CPF
    inexistente volta em 5 ms e para CPF existente em 250 ms: a diferença é o
    bcrypt rodando. Por isso, quando o usuário não é encontrado, o bcrypt roda
    contra um hash descartável. O custo é uma verificação inútil por tentativa
    errada; o ganho é que o tempo deixa de responder a pergunta.

    **Bloqueio progressivo.** Cinco erros bloqueiam por 15 minutos. O contador
    fica na linha do usuário, não em Redis, para que a proteção exista também
    no free tier. A contagem é por conta, não por IP, vale contra ataque
    distribuído, à custa de permitir que alguém bloqueie a conta de outra
    pessoa de propósito, que é o motivo de o bloqueio expirar sozinho em vez de
    exigir intervenção de administrador.
    """
    digitos = cpf_util.apenas_digitos(cpf)
    consulta = select(Usuario).options(selectinload(Usuario.unidade)).where(Usuario.cpf == digitos)
    usuario = (await sessao.execute(consulta)).scalar_one_or_none()

    if usuario is None:
        verificar(senha, _HASH_FALSO)  # gasta o mesmo tempo de propósito
        await auditoria.registrar(
            sessao,
            acao="login.recusado",
            recurso=cpf_util.formatar(digitos),
            resultado=auditoria.RECUSADO,
            servico=SERVICO,
            requisicao=requisicao,
            detalhes={"motivo": "cpf_nao_cadastrado"},
        )
        await sessao.commit()
        raise NaoAutenticado("CPF ou senha inválidos")

    if usuario.esta_bloqueado:
        restante = int((usuario.bloqueado_ate - datetime.now(UTC)).total_seconds() / 60) + 1
        await auditoria.registrar(
            sessao,
            acao="login.recusado",
            recurso=str(usuario.id),
            resultado=auditoria.RECUSADO,
            ator_id=usuario.id,
            ator_nome=usuario.nome,
            ator_cpf=usuario.cpf,
            servico=SERVICO,
            requisicao=requisicao,
            detalhes={"motivo": "conta_bloqueada"},
        )
        await sessao.commit()
        raise NaoAutenticado(f"Conta temporariamente bloqueada. Tente novamente em {restante} min.")

    if not usuario.ativo:
        await auditoria.registrar(
            sessao,
            acao="login.recusado",
            recurso=str(usuario.id),
            resultado=auditoria.RECUSADO,
            ator_id=usuario.id,
            ator_nome=usuario.nome,
            servico=SERVICO,
            requisicao=requisicao,
            detalhes={"motivo": "conta_inativa"},
        )
        await sessao.commit()
        raise NaoAutenticado("CPF ou senha inválidos")

    if not verificar(senha, usuario.senha_hash):
        usuario.tentativas_falhas += 1
        motivo = "senha_incorreta"
        if usuario.tentativas_falhas >= MAX_TENTATIVAS:
            usuario.bloqueado_ate = datetime.now(UTC) + DURACAO_DO_BLOQUEIO
            usuario.tentativas_falhas = 0
            motivo = "bloqueio_acionado"
        await auditoria.registrar(
            sessao,
            acao="login.recusado",
            recurso=str(usuario.id),
            resultado=auditoria.RECUSADO,
            ator_id=usuario.id,
            ator_nome=usuario.nome,
            ator_cpf=usuario.cpf,
            servico=SERVICO,
            requisicao=requisicao,
            detalhes={"motivo": motivo, "tentativas": usuario.tentativas_falhas},
        )
        await sessao.commit()
        raise NaoAutenticado("CPF ou senha inválidos")

    # Senha correta. Zera o contador e, se o custo do bcrypt subiu desde que a
    # senha foi definida, regrava o hash, é o único momento em que a senha em
    # texto claro está disponível para isso.
    usuario.tentativas_falhas = 0
    usuario.bloqueado_ate = None
    usuario.ultimo_login = datetime.now(UTC)
    if precisa_rehash(usuario.senha_hash):
        usuario.senha_hash = gerar_hash(senha)
        log.info("senha.rehash", usuario_id=str(usuario.id))

    identidade = await _montar_identidade(sessao, usuario)

    await auditoria.registrar(
        sessao,
        acao="login.realizado",
        recurso=str(usuario.id),
        ator=identidade,
        servico=SERVICO,
        requisicao=requisicao,
        detalhes={"papeis": list(identidade.papeis)},
    )
    await sessao.commit()
    return identidade


async def autenticar_demonstracao(
    sessao: AsyncSession,
    *,
    papel: Papel,
    requisicao: Any = None,
) -> Identidade:
    """Entrada sem senha no modo de demonstração, assumindo um papel.

    Existe para que quem visita o endereço público possa ver a plataforma do
    ponto de vista de cada um dos seis papéis sem precisar de cadastro, é a
    diferença entre "tela de login" e "demonstração".

    A identidade devolvida é marcada com `demonstracao=True`, e o token carrega
    esse claim. Isso permite ao resto da plataforma recusar ação destrutiva
    vinda de uma sessão de demonstração, e à interface exibir o aviso. A rota
    que chama esta função só existe quando `DEMO_MODE=true`.
    """
    consulta = (
        select(Usuario)
        .options(selectinload(Usuario.unidade))
        .join(PapelUsuario, PapelUsuario.usuario_id == Usuario.id)
        .where(PapelUsuario.papel == papel.value, Usuario.ativo.is_(True))
        .order_by(Usuario.nome)
        .limit(1)
    )
    usuario = (await sessao.execute(consulta)).scalars().first()
    if usuario is None:
        raise NaoEncontrado(
            f"nenhum usuário de demonstração com o papel {papel.value}",
            dica="rode `python -m scripts.seed` para criar o cenário fictício",
        )

    usuario.ultimo_login = datetime.now(UTC)
    base = await _montar_identidade(sessao, usuario)
    identidade = Identidade(
        id=base.id,
        nome=base.nome,
        email=base.email,
        cpf=base.cpf,
        papeis=base.papeis,
        unidade_id=base.unidade_id,
        unidade_sigla=base.unidade_sigla,
        grupos=base.grupos,
        demonstracao=True,
    )

    await auditoria.registrar(
        sessao,
        acao="login.demonstracao",
        recurso=str(usuario.id),
        ator=identidade,
        servico=SERVICO,
        requisicao=requisicao,
        detalhes={"papel_escolhido": papel.value},
    )
    await sessao.commit()
    return identidade


async def obter_usuario(sessao: AsyncSession, usuario_id: uuid.UUID) -> UsuarioDetalhe:
    consulta = (
        select(Usuario)
        .options(
            selectinload(Usuario.unidade),
            # `selectinload` e não `joinedload`: com coleções, o `joinedload`
            # multiplica as linhas do produto cartesiano e o SQLAlchemy precisa
            # deduplicar em memória. Aqui são duas consultas enxutas.
            selectinload(Usuario.vinculos_de_grupo).selectinload(UsuarioGrupo.grupo),
        )
        .where(Usuario.id == usuario_id)
    )
    usuario = (await sessao.execute(consulta)).scalar_one_or_none()
    if usuario is None:
        raise NaoEncontrado("usuário não encontrado", usuario_id=str(usuario_id))

    papeis = await papeis_efetivos(sessao, usuario.id)
    from hub_core.auth.rbac import permissoes_de

    return UsuarioDetalhe(
        id=usuario.id,
        nome=usuario.nome,
        email=usuario.email,
        cpf=usuario.cpf,
        matricula=usuario.matricula,
        ativo=usuario.ativo,
        papeis=list(papeis),
        permissoes=sorted(p.value for p in permissoes_de(papeis)),
        unidade=(UnidadeResumo.model_validate(usuario.unidade) if usuario.unidade else None),
        ultimo_login=usuario.ultimo_login,
        criado_em=usuario.criado_em,
        grupos=[
            GrupoResumo(
                id=vinculo.grupo.id,
                nome=vinculo.grupo.nome,
                tipo=vinculo.grupo.tipo,
                descricao=vinculo.grupo.descricao,
            )
            for vinculo in usuario.vinculos_de_grupo
        ],
    )


async def listar_usuarios(
    sessao: AsyncSession,
    *,
    busca: str | None = None,
    unidade_id: uuid.UUID | None = None,
    papel: str | None = None,
    apenas_ativos: bool = True,
    pagina: int = 1,
    por_pagina: int = 25,
) -> tuple[list[UsuarioResumo], int]:
    consulta = select(Usuario).options(selectinload(Usuario.unidade))

    if apenas_ativos:
        consulta = consulta.where(Usuario.ativo.is_(True))
    if unidade_id is not None:
        consulta = consulta.where(Usuario.unidade_id == unidade_id)
    if papel:
        consulta = consulta.where(
            Usuario.id.in_(select(PapelUsuario.usuario_id).where(PapelUsuario.papel == papel))
        )
    if busca:
        termo = f"%{busca.strip()}%"
        digitos = cpf_util.apenas_digitos(busca)
        # Busca por nome, e-mail ou CPF. O `ilike` em `cpf` só entra quando a
        # pessoa digitou dígitos, para não varrer a coluna toda a cada letra
        # digitada no campo de busca.
        condicoes = [Usuario.nome.ilike(termo), Usuario.email.ilike(termo)]
        if digitos:
            condicoes.append(Usuario.cpf.like(f"{digitos}%"))
        from sqlalchemy import or_

        consulta = consulta.where(or_(*condicoes))

    total = (
        await sessao.execute(select(func.count()).select_from(consulta.subquery()))
    ).scalar_one()

    pagina = max(1, pagina)
    por_pagina = min(max(1, por_pagina), 100)
    consulta = consulta.order_by(Usuario.nome).offset((pagina - 1) * por_pagina).limit(por_pagina)

    usuarios = (await sessao.execute(consulta)).scalars().all()

    itens: list[UsuarioResumo] = []
    for usuario in usuarios:
        papeis = await papeis_efetivos(sessao, usuario.id)
        itens.append(
            UsuarioResumo(
                id=usuario.id,
                nome=usuario.nome,
                email=usuario.email,
                cpf=usuario.cpf,
                matricula=usuario.matricula,
                ativo=usuario.ativo,
                papeis=list(papeis),
                unidade=(
                    UnidadeResumo.model_validate(usuario.unidade) if usuario.unidade else None
                ),
                ultimo_login=usuario.ultimo_login,
            )
        )
    return itens, total


async def criar_usuario(
    sessao: AsyncSession,
    pedido: PedidoDeCriacaoDeUsuario,
    *,
    autor: Identidade,
    requisicao: Any = None,
) -> UsuarioDetalhe:
    if pedido.unidade_id is not None:
        existe = await sessao.get(Unidade, pedido.unidade_id)
        if existe is None:
            raise NaoEncontrado("unidade não encontrada", unidade_id=str(pedido.unidade_id))

    usuario = Usuario(
        cpf=pedido.cpf,
        nome=pedido.nome.strip(),
        email=str(pedido.email).lower(),
        matricula=pedido.matricula,
        senha_hash=gerar_hash(pedido.senha),
        unidade_id=pedido.unidade_id,
    )
    sessao.add(usuario)

    try:
        # `flush` e não `commit`: precisamos do id gerado para as linhas de
        # papel e para a auditoria, mas tudo tem de ser uma transação só.
        await sessao.flush()
    except IntegrityError as erro:
        await sessao.rollback()
        # A constraint violada diz qual campo colidiu, e essa informação é útil
        # no formulário, diferente do login, aqui quem chama já tem permissão
        # de gerenciar usuários, então não há enumeração a proteger.
        texto = str(erro.orig).lower()
        campo = "CPF" if "cpf" in texto else ("e-mail" if "email" in texto else "registro")
        raise Conflito(f"já existe um usuário com este {campo}") from erro

    for papel in dict.fromkeys(pedido.papeis):
        sessao.add(
            PapelUsuario(usuario_id=usuario.id, papel=papel.value, concedido_por_id=autor.id)
        )

    await auditoria.registrar(
        sessao,
        acao="usuario.criado",
        recurso=str(usuario.id),
        ator=autor,
        servico=SERVICO,
        requisicao=requisicao,
        detalhes={
            "nome": usuario.nome,
            "papeis": [p.value for p in pedido.papeis],
            # A senha chega em `pedido` mas não vai para `detalhes`; ainda
            # assim, `_sanear` filtraria a chave caso alguém a inclua por
            # descuido no futuro.
        },
    )
    await sessao.commit()
    await sessao.refresh(usuario)
    return await obter_usuario(sessao, usuario.id)


async def alterar_papeis(
    sessao: AsyncSession,
    usuario_id: uuid.UUID,
    papeis: list[Papel],
    *,
    autor: Identidade,
    requisicao: Any = None,
) -> UsuarioDetalhe:
    """Substitui os papéis diretos da pessoa.

    Impede que um administrador remova o próprio papel de administrador: é o
    erro que deixa a plataforma sem ninguém capaz de concedê-lo de volta,
    exigindo acesso ao banco para consertar.
    """
    usuario = await sessao.get(Usuario, usuario_id)
    if usuario is None:
        raise NaoEncontrado("usuário não encontrado", usuario_id=str(usuario_id))

    if usuario_id == autor.id and Papel.ADMIN not in papeis and autor.e_admin:
        raise SemPermissao(
            "você não pode remover o seu próprio papel de administrador",
            dica="peça a outro administrador para fazer esta alteração",
        )

    anteriores = await papeis_efetivos(sessao, usuario_id)

    from sqlalchemy import delete

    await sessao.execute(delete(PapelUsuario).where(PapelUsuario.usuario_id == usuario_id))
    for papel in dict.fromkeys(papeis):
        sessao.add(
            PapelUsuario(usuario_id=usuario_id, papel=papel.value, concedido_por_id=autor.id)
        )

    await auditoria.registrar(
        sessao,
        acao="usuario.papeis_alterados",
        recurso=str(usuario_id),
        ator=autor,
        servico=SERVICO,
        requisicao=requisicao,
        detalhes={"de": list(anteriores), "para": [p.value for p in papeis]},
    )
    await sessao.commit()
    return await obter_usuario(sessao, usuario_id)


async def definir_ativo(
    sessao: AsyncSession,
    usuario_id: uuid.UUID,
    ativo: bool,
    *,
    autor: Identidade,
    requisicao: Any = None,
) -> UsuarioDetalhe:
    usuario = await sessao.get(Usuario, usuario_id)
    if usuario is None:
        raise NaoEncontrado("usuário não encontrado", usuario_id=str(usuario_id))
    if usuario_id == autor.id and not ativo:
        raise SemPermissao("você não pode desativar a sua própria conta")

    usuario.ativo = ativo
    # Reativar limpa o bloqueio: senão a pessoa é reativada e continua sem
    # conseguir entrar, sem explicação na tela.
    if ativo:
        usuario.bloqueado_ate = None
        usuario.tentativas_falhas = 0

    await auditoria.registrar(
        sessao,
        acao="usuario.ativado" if ativo else "usuario.desativado",
        recurso=str(usuario_id),
        ator=autor,
        servico=SERVICO,
        requisicao=requisicao,
    )
    await sessao.commit()
    return await obter_usuario(sessao, usuario_id)


async def listar_unidades(sessao: AsyncSession) -> list[UnidadeResumo]:
    consulta = select(Unidade).where(Unidade.ativa.is_(True)).order_by(Unidade.sigla)
    unidades = (await sessao.execute(consulta)).scalars().all()
    return [UnidadeResumo.model_validate(u) for u in unidades]


async def listar_grupos(sessao: AsyncSession) -> list[GrupoResumo]:
    consulta = (
        select(
            Grupo,
            func.count(UsuarioGrupo.usuario_id).label("membros"),
        )
        .outerjoin(UsuarioGrupo, UsuarioGrupo.grupo_id == Grupo.id)
        .group_by(Grupo.id)
        .order_by(Grupo.nome)
    )
    linhas = (await sessao.execute(consulta)).all()

    resumos: list[GrupoResumo] = []
    for grupo, membros in linhas:
        papeis = (
            (await sessao.execute(select(PapelGrupo.papel).where(PapelGrupo.grupo_id == grupo.id)))
            .scalars()
            .all()
        )
        resumos.append(
            GrupoResumo(
                id=grupo.id,
                nome=grupo.nome,
                tipo=grupo.tipo,
                descricao=grupo.descricao,
                papeis=sorted(papeis),
                total_membros=membros,
            )
        )
    return resumos
