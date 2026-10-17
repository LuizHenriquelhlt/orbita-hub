"""Testes das regras puras: CPF, RBAC, cron, manifesto, parâmetros, backoff.

Todos sem banco e sem rede. São os testes que rodam em cada salvamento de
arquivo, e por isso precisam ser rápidos.

Cada teste aqui corresponde a uma decisão descrita em comentário no código —
a intenção é que, se alguém "simplificar" uma dessas decisões por engano, um
teste falhe explicando o porquê.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from hub_catalog.ciclo_de_vida import StatusAutomacao, transicoes_de, validar_transicao
from hub_catalog.manifesto import GatilhoDeclarado, ParametroDeclarado, carregar
from hub_catalog.servico import validar_parametros
from hub_core.auth.rbac import (
    Papel,
    Permissao,
    mascarar_cpf,
    mascarar_email,
    permissoes_de,
    tem_permissao,
)
from hub_core.auth.senhas import gerar_hash, precisa_rehash, verificar
from hub_core.broker.base import backoff_segundos
from hub_core.errors import EntradaInvalida, TransicaoInvalida
from hub_core.settings import normalizar_database_url, url_exige_ssl, url_usa_pooler
from hub_execution.agenda import FUSO_PADRAO, ExpressaoCronInvalida, interpretar
from hub_identity import cpf as cpf_util

# ─────────────────────────────────────────────────────────────────────────────
# CPF
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("semente", range(12))
def test_cpf_gerado_e_valido(semente: int) -> None:
    gerado = cpf_util.gerar_cpf_ficticio(semente)
    assert cpf_util.valido(gerado), f"CPF gerado inválido: {gerado}"
    assert gerado.startswith("000"), "a geração usa o prefixo 000 por precaução"


def test_cpf_geracao_e_deterministica() -> None:
    """A semente fixa é o que mantém as credenciais do README válidas."""
    assert cpf_util.gerar_cpf_ficticio("inrh-0") == cpf_util.gerar_cpf_ficticio("inrh-0")


@pytest.mark.parametrize("repetido", ["00000000000", "11111111111", "99999999999"])
def test_cpf_de_digitos_repetidos_e_recusado(repetido: str) -> None:
    """Sequências repetidas passam no módulo 11 por coincidência aritmética.

    É a primeira coisa que se digita para testar um formulário, e aceitar
    `111.111.111-11` como válido é o defeito clássico dessa validação.
    """
    assert not cpf_util.valido(repetido)


def test_cpf_aceita_mascara_e_normaliza() -> None:
    bruto = cpf_util.gerar_cpf_ficticio(1)
    com_mascara = cpf_util.formatar(bruto)
    assert "." in com_mascara
    assert cpf_util.normalizar(com_mascara) == bruto


def test_cpf_invalido_levanta() -> None:
    with pytest.raises(ValueError):
        cpf_util.normalizar("123.456.789-99")


# ─────────────────────────────────────────────────────────────────────────────
# RBAC
# ─────────────────────────────────────────────────────────────────────────────


def test_auditor_nao_dispara_execucao() -> None:
    """Quem fiscaliza não opera. É a separação de funções do papel auditor."""
    assert not tem_permissao([Papel.AUDITOR], Permissao.EXECUCAO_DISPARAR)
    assert tem_permissao([Papel.AUDITOR], Permissao.AUDITORIA_VER)


def test_usuario_comum_nao_ve_auditoria() -> None:
    assert not tem_permissao([Papel.USUARIO], Permissao.AUDITORIA_VER)
    assert not tem_permissao([Papel.USUARIO], Permissao.EXECUCAO_VER_TODAS)


def test_admin_tem_tudo() -> None:
    assert permissoes_de([Papel.ADMIN]) == frozenset(Permissao)


def test_papel_desconhecido_nao_concede_nada() -> None:
    """Token de uma versão mais nova pode trazer papel desconhecido.

    Ignorar concede menos acesso; levantar erro derrubaria a requisição. Em
    dúvida sobre segurança, falhar para o lado de menos permissão.
    """
    assert permissoes_de(["papel-que-nao-existe"]) == frozenset()


def test_papeis_se_somam() -> None:
    juntos = permissoes_de([Papel.USUARIO, Papel.AUDITOR])
    assert juntos == permissoes_de([Papel.AUDITOR]) | permissoes_de([Papel.USUARIO])


def test_so_admin_e_auditor_veem_dado_sensivel() -> None:
    podem = {p for p in Papel if tem_permissao([p], Permissao.DADO_SENSIVEL_VER)}
    assert podem == {Papel.ADMIN, Papel.AUDITOR}


def test_mascara_de_cpf_preserva_os_tres_do_meio() -> None:
    """O suficiente para a pessoa reconhecer a própria linha, sem reconstituir."""
    assert mascarar_cpf("12345678900") == "***.456.***-**"
    assert mascarar_cpf("invalido") == "***.***.***-**"


def test_mascara_de_email() -> None:
    assert mascarar_email("maria.silva@inrh.gov.br") == "ma***@inrh.gov.br"
    assert mascarar_email("sem-arroba") == "***"


def test_mascara_de_email_curto_revela_menos() -> None:
    """Com dois caracteres antes do arroba, mostrar dois seria mostrar tudo.

    A máscara cai para um caractere nesse caso — do contrário, o endereço mais
    curto seria o menos protegido.
    """
    assert mascarar_email("ab@x.br") == "a***@x.br"
    assert mascarar_email("a@x.br") == "a***@x.br"


# ─────────────────────────────────────────────────────────────────────────────
# Senhas
# ─────────────────────────────────────────────────────────────────────────────


def test_hash_e_verificacao() -> None:
    hash_ = gerar_hash("Senha@Muito#Forte123")
    assert verificar("Senha@Muito#Forte123", hash_)
    assert not verificar("outra-senha", hash_)


def test_hash_corrompido_devolve_false_em_vez_de_levantar() -> None:
    """Hash estranho tem de virar "senha errada", não erro 500.

    Caso contrário, a mensagem de erro distingue "usuário existe com hash
    estranho" de "usuário não existe" — e isso é enumeração de conta.
    """
    assert not verificar("qualquer", "nao-e-um-hash")
    assert not verificar("qualquer", "")


def test_senha_longa_demais_e_recusada_explicitamente() -> None:
    """O bcrypt trunca em 72 bytes em silêncio. Aqui o limite é informado."""
    from hub_core.auth.senhas import SenhaMuitoLonga

    with pytest.raises(SenhaMuitoLonga):
        gerar_hash("a" * 100)


def test_rehash_detecta_custo_antigo() -> None:
    assert precisa_rehash("$2b$04$abcdefghijklmnopqrstuv")
    assert not precisa_rehash(gerar_hash("Senha@Forte123"))
    assert precisa_rehash("lixo")


# ─────────────────────────────────────────────────────────────────────────────
# Cron
# ─────────────────────────────────────────────────────────────────────────────


def test_cron_dia_do_mes_ou_dia_da_semana() -> None:
    """A regra do POSIX: com os dois campos restritos, a relação é **OU**.

    `0 0 1 * 1` dispara no dia 1º do mês *e também* em toda segunda-feira.
    Implementar como E produz um agendamento que quase nunca dispara, e o erro
    não gera falha — só silêncio.
    """
    cron = interpretar("0 0 1 * 1")
    base = datetime(2026, 10, 17, 12, 0, tzinfo=FUSO_PADRAO)  # sábado

    ocorrencias = []
    momento = base
    for _ in range(5):
        momento = cron.proxima_ocorrencia(depois_de=momento)
        assert momento is not None
        ocorrencias.append(momento.astimezone(FUSO_PADRAO))

    dias = {o.day for o in ocorrencias}
    semanas = {o.weekday() for o in ocorrencias}
    assert 1 in dias, "deveria disparar no dia 1º"
    assert 0 in semanas, "deveria disparar às segundas"


def test_cron_domingo_como_0_ou_7() -> None:
    base = datetime(2026, 10, 17, 12, 0, tzinfo=FUSO_PADRAO)
    assert interpretar("0 9 * * 0").proxima_ocorrencia(depois_de=base) == interpretar(
        "0 9 * * 7"
    ).proxima_ocorrencia(depois_de=base)


def test_cron_29_de_fevereiro_encontra_ano_bissexto() -> None:
    """A expressão válida com o maior intervalo possível entre ocorrências.

    É o que define a janela de busca de 4 anos em `agenda._MAX_MINUTOS`.
    """
    base = datetime(2026, 3, 1, 0, 0, tzinfo=FUSO_PADRAO)
    proxima = interpretar("0 0 29 2 *").proxima_ocorrencia(depois_de=base)
    assert proxima is not None
    local = proxima.astimezone(FUSO_PADRAO)
    assert (local.year, local.month, local.day) == (2028, 2, 29)


def test_cron_interpreta_no_fuso_de_sao_paulo() -> None:
    """ "0 6 * * *" é seis da manhã no horário de quem recebe, não em UTC."""
    base = datetime(2026, 10, 17, 12, 0, tzinfo=FUSO_PADRAO)
    proxima = interpretar("0 6 * * *").proxima_ocorrencia(depois_de=base)
    assert proxima is not None
    assert proxima.astimezone(FUSO_PADRAO).hour == 6


@pytest.mark.parametrize(
    "expressao",
    [
        "0 6 * *",  # quatro campos
        "60 * * * *",  # minuto fora da faixa
        "0 25 * * *",  # hora fora da faixa
        "0 6 32 * *",  # dia fora da faixa
        "* * * 13 *",  # mês fora da faixa
        "0 9 * * 8",  # dia da semana fora da faixa
        "0-X * * * *",  # não numérico
        "*/0 * * * *",  # passo zero
        "10-5 * * * *",  # faixa invertida
    ],
)
def test_cron_invalido_e_recusado(expressao: str) -> None:
    with pytest.raises(ExpressaoCronInvalida):
        interpretar(expressao)


def test_cron_descreve_em_portugues() -> None:
    assert interpretar("0 6 * * *").descrever() == "todos os dias às 06:00"
    assert "dia 1" in interpretar("0 6 1 * *").descrever()


# ─────────────────────────────────────────────────────────────────────────────
# Manifesto
# ─────────────────────────────────────────────────────────────────────────────

MANIFESTO_MINIMO = """
apiVersion: orbita.inrh/v1
kind: Automacao
metadata:
  nome: teste-automacao
  titulo: Automação de teste
  versao: 1.0.0
  categoria: relatorios
  descricao: Uma descrição com mais de quarenta caracteres para poder publicar.
spec:
  tipo: batch
  imagem: local/teste
  gatilhos:
    - tipo: manual
"""


def test_manifesto_minimo_e_aceito() -> None:
    manifesto = carregar(MANIFESTO_MINIMO)
    assert manifesto.slug == "teste-automacao"
    assert manifesto.spec.recursos.memoria_mb == 512


def test_manifesto_recusa_campo_desconhecido() -> None:
    """Campo desconhecido é erro, não é ignorado.

    Manifesto com erro de digitação que "funciona pela metade" é pior do que
    manifesto recusado: a automação entra no catálogo com uma configuração que
    ninguém pediu.
    """
    with pytest.raises(EntradaInvalida):
        carregar(MANIFESTO_MINIMO.replace("  tipo: batch", "  tipo: batch\n  inventado: 1"))


def test_manifesto_recusa_yaml_perigoso() -> None:
    """`safe_load` impede que enviar um manifesto vire executar código."""
    with pytest.raises(EntradaInvalida):
        carregar("!!python/object/apply:os.system ['echo invadido']")


def test_manifesto_recusa_valor_de_segredo() -> None:
    perigoso = (
        MANIFESTO_MINIMO
        + """
  variaveis_ambiente:
    - nome: TOKEN
      valor: segredo-em-texto-claro
"""
    )
    with pytest.raises(EntradaInvalida) as erro:
        carregar(perigoso)
    problemas = str(erro.value.detalhes.get("problemas", ""))
    assert "segredo_ref" in problemas


def test_parametro_opcao_exige_opcoes() -> None:
    """Validador **de modelo**, não de campo.

    No Pydantic v2, `@field_validator` não roda quando o campo cai no valor
    padrão — e `tipo: opcao` sem a chave `opcoes` passaria despercebido.
    """
    with pytest.raises(ValueError, match="ao menos uma opção"):
        ParametroDeclarado(nome="r", rotulo="R", tipo="opcao")


def test_parametro_nao_opcao_recusa_opcoes() -> None:
    with pytest.raises(ValueError, match="não aceita opções"):
        ParametroDeclarado(
            nome="r", rotulo="R", tipo="texto", opcoes=[{"valor": "a", "rotulo": "A"}]
        )


def test_gatilho_cron_sem_expressao_e_recusado() -> None:
    """Mesmo motivo: o campo ausente nunca acionaria um validador de campo."""
    with pytest.raises(ValueError, match="exige 'expressao'"):
        GatilhoDeclarado(tipo="cron")


def test_gatilho_manual_nao_aceita_expressao() -> None:
    with pytest.raises(ValueError, match="não aceita"):
        GatilhoDeclarado(tipo="manual", expressao="0 6 * * *")


def test_webhook_exige_caminho_absoluto() -> None:
    with pytest.raises(ValueError, match="começar com '/'"):
        GatilhoDeclarado(tipo="webhook", expressao="sem-barra")


def test_manifestos_do_repositorio_sao_validos() -> None:
    """Os três exemplos precisam atender ao contrato que a plataforma publica.

    Sem isto, um manifesto de exemplo quebrado só apareceria no `seed`.
    """
    from pathlib import Path

    raiz = Path(__file__).resolve().parents[1] / "automations"
    arquivos = sorted(raiz.glob("*/automation.yaml"))
    assert arquivos, "nenhum automation.yaml encontrado em automations/"

    for arquivo in arquivos:
        manifesto = carregar(arquivo.read_text(encoding="utf-8"))
        assert manifesto.slug == arquivo.parent.name, (
            f"{arquivo}: o nome no manifesto deve ser igual ao do diretório"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Validação de parâmetros
# ─────────────────────────────────────────────────────────────────────────────

DECLARADOS = [
    {"nome": "competencia", "rotulo": "Competência", "tipo": "mes", "obrigatorio": True},
    {"nome": "limite", "rotulo": "Limite", "tipo": "numero_inteiro", "minimo": 5, "maximo": 95},
    {"nome": "ativo", "rotulo": "Ativo", "tipo": "booleano"},
    {
        "nome": "regiao",
        "rotulo": "Região",
        "tipo": "opcao",
        "opcoes": [{"valor": "norte", "rotulo": "Norte"}],
    },
]


def test_parametros_descarta_o_que_nao_foi_declarado() -> None:
    """Chave não declarada não chega ao ambiente da automação."""
    saneado = validar_parametros(DECLARADOS, {"competencia": "2026-09", "injetado": "x"})
    assert "injetado" not in saneado
    assert saneado["competencia"] == "2026-09"


def test_parametro_obrigatorio_ausente_e_erro() -> None:
    with pytest.raises(EntradaInvalida) as erro:
        validar_parametros(DECLARADOS, {})
    campos = erro.value.detalhes["campos"]
    assert any(c["campo"] == "competencia" for c in campos)


def test_numero_decimal_aceita_virgula() -> None:
    """É o separador que o teclado em português produz."""
    declarados = [{"nome": "v", "rotulo": "V", "tipo": "numero_decimal"}]
    assert validar_parametros(declarados, {"v": "3,5"})["v"] == 3.5


def test_faixa_numerica_e_respeitada() -> None:
    with pytest.raises(EntradaInvalida):
        validar_parametros(DECLARADOS, {"competencia": "2026-09", "limite": "999"})


def test_opcao_invalida_lista_as_validas() -> None:
    with pytest.raises(EntradaInvalida) as erro:
        validar_parametros(DECLARADOS, {"competencia": "2026-09", "regiao": "sul"})
    assert "norte" in str(erro.value.detalhes["campos"])


def test_mes_normaliza_para_dois_digitos() -> None:
    assert validar_parametros(DECLARADOS, {"competencia": "2026-9"})["competencia"] == "2026-09"


def test_booleano_aceita_variantes_em_portugues() -> None:
    for entrada in ("sim", "true", "1", "on"):
        assert validar_parametros(DECLARADOS, {"competencia": "2026-01", "ativo": entrada})["ativo"]
    assert not validar_parametros(DECLARADOS, {"competencia": "2026-01", "ativo": "nao"})["ativo"]


# ─────────────────────────────────────────────────────────────────────────────
# Ciclo de vida
# ─────────────────────────────────────────────────────────────────────────────


def test_rascunho_nao_vai_direto_para_publicada() -> None:
    with pytest.raises(TransicaoInvalida) as erro:
        validar_transicao(StatusAutomacao.RASCUNHO, StatusAutomacao.PUBLICADA)
    assert "homologacao" in str(erro.value.detalhes["destinos_possiveis"])


def test_transicao_para_o_mesmo_estado_e_recusada() -> None:
    with pytest.raises(TransicaoInvalida):
        validar_transicao(StatusAutomacao.PUBLICADA, StatusAutomacao.PUBLICADA)


def test_arquivada_e_terminal() -> None:
    assert transicoes_de(StatusAutomacao.ARQUIVADA) == ()


def test_depreciada_ainda_executa_e_arquivada_nao() -> None:
    """Depreciar não pode cortar a execução: a substituta pode ainda estar em
    homologação, e haveria um intervalo sem nenhuma das duas funcionando."""
    assert StatusAutomacao.DEPRECIADA.executavel
    assert not StatusAutomacao.ARQUIVADA.executavel


def test_republicacao_e_possivel() -> None:
    """Desfazer uma depreciação feita por engano, sem recriar a automação."""
    transicao = validar_transicao(StatusAutomacao.DEPRECIADA, StatusAutomacao.PUBLICADA)
    assert transicao.permissao == "automacao:publicar"


# ─────────────────────────────────────────────────────────────────────────────
# Fila e configuração
# ─────────────────────────────────────────────────────────────────────────────


def test_backoff_cresce_e_tem_teto() -> None:
    """Sem teto, a quinta tentativa cairia em horas de espera."""
    valores = [backoff_segundos(n) for n in range(1, 10)]
    assert valores[:4] == [5, 10, 20, 40]
    assert max(valores) == 600
    assert valores == sorted(valores), "o backoff nunca diminui"


@pytest.mark.parametrize(
    ("entrada", "esquema_esperado"),
    [
        ("postgres://u:s@h/db", "postgresql+asyncpg"),
        ("postgresql://u:s@h/db", "postgresql+asyncpg"),
        ("postgresql+psycopg2://u:s@h/db", "postgresql+asyncpg"),
        ("postgresql+asyncpg://u:s@h/db", "postgresql+asyncpg"),
    ],
)
def test_url_do_banco_recebe_driver_async(entrada: str, esquema_esperado: str) -> None:
    assert normalizar_database_url(entrada).startswith(esquema_esperado)


def test_parametros_de_libpq_sao_removidos_da_url() -> None:
    """O asyncpg não conhece `sslmode` nem `channel_binding` e estoura com eles."""
    bruta = "postgresql://u:s@ep-x.neon.tech/db?sslmode=require&channel_binding=require"
    normalizada = normalizar_database_url(bruta)
    assert "sslmode" not in normalizada
    assert "channel_binding" not in normalizada
    assert url_exige_ssl(bruta), "a intenção de TLS precisa ser preservada"


def test_endpoint_pooled_do_neon_e_detectado() -> None:
    """Atrás de PgBouncer é preciso desligar o cache de prepared statements."""
    assert url_usa_pooler("postgresql://u:s@ep-abc-pooler.sa-east-1.aws.neon.tech/db")
    assert not url_usa_pooler("postgresql://u:s@ep-abc.sa-east-1.aws.neon.tech/db")
