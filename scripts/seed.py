"""Popula o banco com o cenário fictício do INRH.

    python -m scripts.seed              # estrutura, pessoas e automações
    python -m scripts.seed --historico  # inclui 60 dias de execuções passadas

Idempotente: rodar duas vezes não duplica nada. É o que permite usá-lo como
parte do `make up` e também para recarregar o cenário depois de mexer no banco.

## O que é o INRH

Instituto Nacional de Recursos Hídricos — um órgão federal **fictício**, criado
para este projeto. Nenhuma unidade, pessoa, outorga ou processo corresponde a
algo real. Os CPFs têm dígitos verificadores válidos (necessário para o fluxo
de login funcionar) e usam o prefixo `000`, que não é atribuído a pessoa
física — ver `hub_identity.cpf`.

## Por que o histórico é opcional

Sem histórico, a plataforma sobe limpa e cada execução que o visitante dispara
é visível e isolada — bom para entender o mecanismo. Com histórico, o painel
tem o que mostrar: série temporal, taxa de sucesso, automações mais usadas.
O endereço público roda com `--historico`; quem clona o repositório decide.
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from hub_catalog import servico as servico_catalog  # noqa: E402
from hub_catalog.modelos import Automacao  # noqa: E402
from hub_core import db  # noqa: E402
from hub_core.auth import Papel, gerar_hash  # noqa: E402
from hub_core.auth.tokens import Identidade  # noqa: E402
from hub_core.executor.base import StatusExecucao  # noqa: E402
from hub_core.logging import configurar_logging, obter_logger  # noqa: E402
from hub_core.settings import obter_settings  # noqa: E402
from hub_execution.modelos import Execucao, ExecucaoLog, ExecucaoMetrica  # noqa: E402
from hub_identity import cpf as cpf_util  # noqa: E402
from hub_identity.modelos import (  # noqa: E402
    Grupo,
    PapelGrupo,
    PapelUsuario,
    Unidade,
    Usuario,
    UsuarioGrupo,
)

log = obter_logger("seed")

# Senha única para todas as contas do cenário. Pública por desenho: é um
# ambiente de demonstração com dado sintético, e esconder a senha de um
# ambiente assim só dificulta a vida de quem clona o repositório.
SENHA_DEMO = "Orbita@Demo2026"


@dataclass(frozen=True, slots=True)
class UnidadeSemente:
    sigla: str
    nome: str
    tipo: str
    pai: str | None = None
    regiao: str | None = None


UNIDADES = (
    UnidadeSemente("INRH", "Instituto Nacional de Recursos Hídricos", "diretoria"),
    UnidadeSemente("DIROP", "Diretoria de Operações", "diretoria", "INRH"),
    UnidadeSemente("DIRTI", "Diretoria de Tecnologia e Informação", "diretoria", "INRH"),
    UnidadeSemente("CGOUT", "Coordenação-Geral de Outorgas", "coordenacao", "DIROP"),
    UnidadeSemente("CGFIS", "Coordenação-Geral de Fiscalização", "coordenacao", "DIROP"),
    UnidadeSemente("CGESP", "Coordenação-Geral de Gestão de Pessoas", "coordenacao", "INRH"),
    UnidadeSemente("NUDAD", "Núcleo de Dados e Automação", "nucleo", "DIRTI"),
    UnidadeSemente(
        "SRNOR", "Superintendência Regional Norte", "superintendencia", "DIROP", "Norte"
    ),
    UnidadeSemente(
        "SRNDE", "Superintendência Regional Nordeste", "superintendencia", "DIROP", "Nordeste"
    ),
    UnidadeSemente(
        "SRSUD", "Superintendência Regional Sudeste", "superintendencia", "DIROP", "Sudeste"
    ),
)


@dataclass(frozen=True, slots=True)
class GrupoSemente:
    nome: str
    descricao: str
    tipo: str
    unidade: str
    papeis: tuple[Papel, ...]


GRUPOS = (
    GrupoSemente(
        "Equipe de Automação",
        "Desenvolve e publica as automações da plataforma.",
        "equipe",
        "NUDAD",
        (Papel.SUPERVISOR,),
    ),
    GrupoSemente(
        "Analistas de Outorga",
        "Operam as automações do fluxo de outorgas.",
        "equipe",
        "CGOUT",
        (Papel.OPERADOR,),
    ),
    GrupoSemente(
        "Controle Interno",
        "Acompanha a trilha de auditoria. Não opera automações.",
        "equipe",
        "INRH",
        (Papel.AUDITOR,),
    ),
)


@dataclass(frozen=True, slots=True)
class PessoaSemente:
    nome: str
    email: str
    matricula: str
    unidade: str
    papeis: tuple[Papel, ...]
    grupos: tuple[str, ...] = ()


# Uma pessoa por papel, para que a entrada por demonstração encontre alguém
# com cada um dos seis — ver `hub_identity.servico.autenticar_demonstracao`.
PESSOAS = (
    PessoaSemente(
        "Helena Vasques Albuquerque",
        "helena.albuquerque@inrh.gov.br",
        "1900101",
        "DIRTI",
        (Papel.ADMIN,),
        ("Equipe de Automação",),
    ),
    PessoaSemente(
        "Rodrigo Teixeira Lontra",
        "rodrigo.lontra@inrh.gov.br",
        "1900202",
        "DIROP",
        (Papel.GESTOR,),
    ),
    PessoaSemente(
        "Marina Caldeira Prado",
        "marina.prado@inrh.gov.br",
        "1900303",
        "NUDAD",
        (Papel.SUPERVISOR,),
        ("Equipe de Automação",),
    ),
    PessoaSemente(
        "Bruno Sarmento Vilela",
        "bruno.vilela@inrh.gov.br",
        "1900404",
        "CGOUT",
        (Papel.OPERADOR,),
        ("Analistas de Outorga",),
    ),
    PessoaSemente(
        "Ana Lúcia Resende Mota",
        "ana.mota@inrh.gov.br",
        "1900505",
        "SRSUD",
        (Papel.USUARIO,),
    ),
    PessoaSemente(
        "Cláudio Nogueira Faria",
        "claudio.faria@inrh.gov.br",
        "1900606",
        "INRH",
        (Papel.AUDITOR,),
        ("Controle Interno",),
    ),
    # Pessoas adicionais, para que as listas e os filtros tenham o que mostrar.
    PessoaSemente(
        "Patrícia Olinto Bezerra",
        "patricia.bezerra@inrh.gov.br",
        "1900707",
        "SRNDE",
        (Papel.OPERADOR,),
        ("Analistas de Outorga",),
    ),
    PessoaSemente(
        "Gustavo Henrique Amarante",
        "gustavo.amarante@inrh.gov.br",
        "1900808",
        "SRNOR",
        (Papel.OPERADOR,),
    ),
    PessoaSemente(
        "Letícia Barroso Queiroz",
        "leticia.queiroz@inrh.gov.br",
        "1900909",
        "CGFIS",
        (Papel.USUARIO,),
    ),
    PessoaSemente(
        "Fernando Dutra Mascarenhas",
        "fernando.mascarenhas@inrh.gov.br",
        "1901010",
        "CGESP",
        (Papel.GESTOR,),
    ),
)

MANIFESTOS = (
    "relatorio-outorgas-mensal",
    "monitor-reservatorios",
    "sincroniza-cadastro-funcional",
)


# ─────────────────────────────────────────────────────────────────────────────


async def semear_unidades(sessao: AsyncSession) -> dict[str, Unidade]:
    """Cria as unidades em duas passagens: primeiro todas, depois a hierarquia.

    Em uma passagem só, uma unidade filha precisaria que a mãe já existisse —
    o que obrigaria a ordenar a lista topologicamente à mão. Com duas, a ordem
    da lista é livre e a hierarquia é resolvida no fim.
    """
    por_sigla: dict[str, Unidade] = {}

    existentes = {u.sigla: u for u in (await sessao.execute(select(Unidade))).scalars().all()}

    for semente in UNIDADES:
        unidade = existentes.get(semente.sigla)
        if unidade is None:
            unidade = Unidade(
                sigla=semente.sigla,
                nome=semente.nome,
                tipo=semente.tipo,
                regiao=semente.regiao,
            )
            sessao.add(unidade)
        else:
            unidade.nome = semente.nome
            unidade.tipo = semente.tipo
            unidade.regiao = semente.regiao
        por_sigla[semente.sigla] = unidade

    await sessao.flush()

    for semente in UNIDADES:
        if semente.pai:
            por_sigla[semente.sigla].unidade_pai_id = por_sigla[semente.pai].id

    await sessao.flush()
    log.info("seed.unidades", total=len(por_sigla))
    return por_sigla


async def semear_grupos(sessao: AsyncSession, unidades: dict[str, Unidade]) -> dict[str, Grupo]:
    por_nome: dict[str, Grupo] = {
        g.nome: g for g in (await sessao.execute(select(Grupo))).scalars().all()
    }

    for semente in GRUPOS:
        grupo = por_nome.get(semente.nome)
        if grupo is None:
            grupo = Grupo(
                nome=semente.nome,
                descricao=semente.descricao,
                tipo=semente.tipo,
                unidade_id=unidades[semente.unidade].id,
            )
            sessao.add(grupo)
            await sessao.flush()
            por_nome[semente.nome] = grupo

        papeis_atuais = {
            p.papel
            for p in (
                await sessao.execute(select(PapelGrupo).where(PapelGrupo.grupo_id == grupo.id))
            ).scalars()
        }
        for papel in semente.papeis:
            if papel.value not in papeis_atuais:
                sessao.add(PapelGrupo(grupo_id=grupo.id, papel=papel.value))

    await sessao.flush()
    log.info("seed.grupos", total=len(por_nome))
    return por_nome


async def semear_pessoas(
    sessao: AsyncSession, unidades: dict[str, Unidade], grupos: dict[str, Grupo]
) -> list[Usuario]:
    senha_hash = gerar_hash(SENHA_DEMO)
    criados: list[Usuario] = []

    existentes = {u.email: u for u in (await sessao.execute(select(Usuario))).scalars().all()}

    for indice, semente in enumerate(PESSOAS):
        # CPF determinístico pela posição na lista: o mesmo cenário sempre
        # produz os mesmos CPFs, então a credencial impressa no README continua
        # valendo depois de recarregar o banco.
        cpf = cpf_util.gerar_cpf_ficticio(f"inrh-{indice}")

        usuario = existentes.get(semente.email)
        if usuario is None:
            usuario = Usuario(
                cpf=cpf,
                nome=semente.nome,
                email=semente.email,
                matricula=semente.matricula,
                senha_hash=senha_hash,
                unidade_id=unidades[semente.unidade].id,
            )
            sessao.add(usuario)
            await sessao.flush()
        else:
            usuario.nome = semente.nome
            usuario.matricula = semente.matricula
            usuario.unidade_id = unidades[semente.unidade].id
            usuario.ativo = True
            usuario.bloqueado_ate = None
            usuario.tentativas_falhas = 0

        papeis_atuais = {
            p.papel
            for p in (
                await sessao.execute(
                    select(PapelUsuario).where(PapelUsuario.usuario_id == usuario.id)
                )
            ).scalars()
        }
        for papel in semente.papeis:
            if papel.value not in papeis_atuais:
                sessao.add(PapelUsuario(usuario_id=usuario.id, papel=papel.value))

        vinculos_atuais = {
            v.grupo_id
            for v in (
                await sessao.execute(
                    select(UsuarioGrupo).where(UsuarioGrupo.usuario_id == usuario.id)
                )
            ).scalars()
        }
        for nome_grupo in semente.grupos:
            grupo = grupos.get(nome_grupo)
            if grupo is not None and grupo.id not in vinculos_atuais:
                sessao.add(UsuarioGrupo(usuario_id=usuario.id, grupo_id=grupo.id))

        criados.append(usuario)

    await sessao.flush()
    log.info("seed.pessoas", total=len(criados))
    return criados


async def semear_automacoes(sessao: AsyncSession, admin: Usuario) -> list[str]:
    """Importa os manifestos de `automations/` pelo caminho normal de importação.

    Usa `servico_catalog.importar_manifesto`, a mesma função que a rota de
    importação chama. O seed exercita o caminho real em vez de inserir linhas
    direto na tabela — se a importação quebrar, o seed quebra, e isso é
    desejável.
    """
    from hub_identity.servico import grupos_de, papeis_efetivos

    identidade = Identidade(
        id=admin.id,
        nome=admin.nome,
        email=admin.email,
        cpf=admin.cpf,
        papeis=await papeis_efetivos(sessao, admin.id),
        unidade_id=admin.unidade_id,
        grupos=await grupos_de(sessao, admin.id),
    )

    siglas = {u.sigla.upper(): u.id for u in (await sessao.execute(select(Unidade))).scalars()}

    async def resolver_unidade(sigla: str) -> object:
        return siglas.get(sigla.upper())

    importadas: list[str] = []
    for nome in MANIFESTOS:
        caminho = RAIZ / "automations" / nome / "automation.yaml"
        if not caminho.is_file():
            log.warning("seed.manifesto_ausente", caminho=str(caminho))
            continue

        resultado = await servico_catalog.importar_manifesto(
            sessao,
            caminho.read_text(encoding="utf-8"),
            autor=identidade,
            publicar=nome != "sincroniza-cadastro-funcional",
            resolver_unidade=resolver_unidade,
        )
        importadas.append(resultado.slug)
        log.info(
            "seed.automacao",
            slug=resultado.slug,
            criada=resultado.criada,
            status=resultado.status,
            avisos=resultado.avisos,
        )

    # A que falha fica em homologação: assim o catálogo mostra os dois estados,
    # e ela não aparece para quem não pode editar — o que também demonstra a
    # regra de visibilidade por status.
    pendente = (
        await sessao.execute(
            select(Automacao).where(Automacao.slug == "sincroniza-cadastro-funcional")
        )
    ).scalar_one_or_none()
    if pendente is not None and pendente.status == "rascunho":
        pendente.status = "homologacao"

    await sessao.flush()
    return importadas


async def semear_historico(sessao: AsyncSession, pessoas: list[Usuario]) -> int:
    """Gera execuções passadas para o painel ter o que mostrar.

    Os números são plausíveis por construção, não aleatórios uniformes: mais
    execuções em dia de semana, taxa de sucesso alta mas não perfeita, duração
    coerente com o `consumo_estimado` de cada automação. Um painel de
    demonstração com 50% de falha ou com volume idêntico todos os dias denuncia
    que o dado foi inventado sem cuidado.
    """
    ja_existe = (await sessao.execute(select(Execucao.id).limit(1))).first()
    if ja_existe:
        log.info("seed.historico_ja_existe")
        return 0

    # `selectinload` é obrigatório aqui, não otimização: o laço abaixo lê
    # `automacao.versoes`, e em SQLAlchemy assíncrono um relacionamento
    # carregado sob demanda tenta fazer IO fora do greenlet e estoura com
    # `MissingGreenlet`. A regra prática: em código `async`, todo
    # relacionamento que vai ser lido tem de ser pedido na consulta.
    from sqlalchemy.orm import selectinload

    automacoes = {
        a.slug: a
        for a in (await sessao.execute(select(Automacao).options(selectinload(Automacao.versoes))))
        .scalars()
        .all()
    }
    if not automacoes:
        return 0

    aleatorio = random.Random("orbita-historico-v1")
    agora = datetime.now(UTC)
    total = 0

    perfis = {
        "relatorio-outorgas-mensal": {"por_dia": (0, 3), "sucesso": 0.94, "dur": (8, 30)},
        "monitor-reservatorios": {"por_dia": (1, 4), "sucesso": 0.97, "dur": (25, 55)},
        "sincroniza-cadastro-funcional": {"por_dia": (0, 1), "sucesso": 0.15, "dur": (12, 35)},
    }

    for dias_atras in range(59, -1, -1):
        dia = agora - timedelta(days=dias_atras)
        fim_de_semana = dia.weekday() >= 5

        for slug, automacao in automacoes.items():
            perfil = perfis.get(slug, {"por_dia": (0, 2), "sucesso": 0.9, "dur": (10, 40)})
            minimo, maximo = perfil["por_dia"]  # type: ignore[index]
            quantidade = aleatorio.randint(minimo, maximo)
            if fim_de_semana:
                quantidade = max(0, quantidade - 2)

            for _ in range(quantidade):
                pessoa = aleatorio.choice(pessoas)
                sucesso = aleatorio.random() < float(perfil["sucesso"])  # type: ignore[arg-type]
                agendada = aleatorio.random() < 0.35

                dur_min, dur_max = perfil["dur"]  # type: ignore[misc]
                duracao_s = aleatorio.uniform(float(dur_min), float(dur_max))
                espera_s = aleatorio.uniform(0.3, 6.0)

                hora = 6 if agendada else aleatorio.randint(8, 19)
                enfileirada = dia.replace(
                    hour=hora, minute=aleatorio.randint(0, 59), second=0, microsecond=0
                )
                iniciada = enfileirada + timedelta(seconds=espera_s)
                finalizada = iniciada + timedelta(seconds=duracao_s)

                if sucesso:
                    status = StatusExecucao.SUCESSO
                else:
                    status = (
                        StatusExecucao.TIMEOUT
                        if aleatorio.random() < 0.15
                        else StatusExecucao.FALHA
                    )

                execucao = Execucao(
                    automacao_id=automacao.id,
                    automacao_slug=slug,
                    automacao_nome=automacao.nome,
                    automacao_versao=(automacao.versoes[0].versao if automacao.versoes else None),
                    usuario_id=None if agendada else pessoa.id,
                    usuario_nome="Agendador" if agendada else pessoa.nome,
                    usuario_cpf=None if agendada else pessoa.cpf,
                    unidade_id=None if agendada else pessoa.unidade_id,
                    status=status.value,
                    origem="agendada" if agendada else "manual",
                    fila="execucoes.lote" if agendada else "execucoes.padrao",
                    prioridade=1 if agendada else 5,
                    executor="simulado",
                    enfileirada_em=enfileirada,
                    iniciada_em=iniciada,
                    finalizada_em=finalizada,
                    duracao_ms=int(duracao_s * 1000),
                    espera_ms=int(espera_s * 1000),
                    parametros={"competencia": enfileirada.strftime("%Y-%m")},
                    resultado_resumo=(
                        "Execução concluída" if sucesso else "Falhou durante a execução"
                    ),
                    erro=None if sucesso else "limite de divergências excedido",
                    codigo_saida=0 if sucesso else 2,
                    consumo={
                        "simulado": True,
                        "duracao_segundos": round(duracao_s, 2),
                        "memoria_pico_mb": aleatorio.randint(70, 260),
                    },
                )
                sessao.add(execucao)
                await sessao.flush()

                # Algumas linhas de log, para que abrir uma execução antiga
                # mostre algo em vez de uma tela vazia.
                for sequencia, (nivel, mensagem) in enumerate(
                    _log_de_exemplo(slug, sucesso), start=1
                ):
                    sessao.add(
                        ExecucaoLog(
                            execucao_id=execucao.id,
                            sequencia=sequencia,
                            nivel=nivel,
                            mensagem=mensagem,
                            ocorrido_em=iniciada + timedelta(seconds=sequencia * 0.8),
                        )
                    )

                if sucesso:
                    for nome, valor in _metricas_de_exemplo(slug, aleatorio).items():
                        sessao.add(ExecucaoMetrica(execucao_id=execucao.id, nome=nome, valor=valor))

                total += 1

    await sessao.flush()
    log.info("seed.historico", execucoes=total)
    return total


def _log_de_exemplo(slug: str, sucesso: bool) -> list[tuple[str, str]]:
    comum = [("INFO", "preparando ambiente de execução")]
    if slug == "relatorio-outorgas-mensal":
        comum += [
            ("INFO", "lendo outorgas.csv"),
            ("WARNING", "28 linha(s) rejeitada(s) — ver outorgas-rejeitadas.csv"),
            ("INFO", "1256 outorga(s) após os filtros"),
        ]
    elif slug == "monitor-reservatorios":
        comum += [
            ("INFO", "42 reservatórios monitorados"),
            ("WARNING", "3 reservatórios sem medição nas últimas 6 h"),
        ]
    else:
        comum += [
            ("INFO", "1 284 matrículas com alteração"),
            ("WARNING", "102 matrículas sem correspondência local"),
        ]

    if sucesso:
        comum.append(("INFO", "execução concluída"))
    else:
        comum.append(("ERROR", "limite de divergências excedido — execução abortada"))
    return comum


def _metricas_de_exemplo(slug: str, aleatorio: random.Random) -> dict[str, float]:
    if slug == "relatorio-outorgas-mensal":
        return {
            "linhas_lidas": 1420,
            "linhas_validas": float(aleatorio.randint(1380, 1400)),
            "linhas_rejeitadas": float(aleatorio.randint(20, 40)),
            "vazao_total_m3_s": round(aleatorio.uniform(2800, 3300), 3),
        }
    if slug == "monitor-reservatorios":
        return {
            "reservatorios_consultados": 42,
            "reservatorios_em_alerta": float(aleatorio.randint(3, 11)),
            "volume_util_medio_pct": round(aleatorio.uniform(38, 62), 1),
        }
    return {"matriculas_recebidas": float(aleatorio.randint(900, 1500))}


# ─────────────────────────────────────────────────────────────────────────────


async def executar(com_historico: bool) -> None:
    cfg = obter_settings()
    configurar_logging(json=cfg.log_json, nivel=cfg.log_nivel)
    db.inicializar(cfg)

    async with db.fabrica_de_sessoes()() as sessao:
        unidades = await semear_unidades(sessao)
        grupos = await semear_grupos(sessao, unidades)
        pessoas = await semear_pessoas(sessao, unidades, grupos)
        await sessao.commit()

        admin = next(p for p in pessoas if p.email == "helena.albuquerque@inrh.gov.br")
        automacoes = await semear_automacoes(sessao, admin)
        await sessao.commit()

        if com_historico:
            await semear_historico(sessao, pessoas)
            await sessao.commit()

    await db.encerrar()

    print()
    print("  Cenário fictício do INRH carregado.")
    print()
    print(f"  {len(unidades)} unidades, {len(grupos)} grupos, {len(pessoas)} pessoas")
    print(f"  {len(automacoes)} automações: {', '.join(automacoes)}")
    print()
    print("  Entrar com CPF e senha:")
    print()
    for indice, semente in enumerate(PESSOAS[:6]):
        cpf = cpf_util.formatar(cpf_util.gerar_cpf_ficticio(f"inrh-{indice}"))
        papel = semente.papeis[0].value
        print(f"    {papel:11s} {cpf}   {semente.nome}")
    print()
    print(f"  Senha de todas: {SENHA_DEMO}")
    print()
    print("  Com DEMO_MODE=true, a tela de entrada também oferece")
    print("  'entrar como demonstração' e troca de papel sem senha.")
    print()


def main() -> None:
    analisador = argparse.ArgumentParser(description=__doc__)
    analisador.add_argument(
        "--historico",
        action="store_true",
        help="gera 60 dias de execuções passadas, para o painel ter o que mostrar",
    )
    argumentos = analisador.parse_args()
    asyncio.run(executar(com_historico=argumentos.historico))


if __name__ == "__main__":
    main()
