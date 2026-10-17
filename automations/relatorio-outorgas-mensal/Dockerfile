# Imagem da automacao de exemplo.
#
# Construida por `make imagens`, a partir da RAIZ do repositorio (o contexto
# precisa enxergar `sdk/`):
#
#   docker build -f automations/relatorio-outorgas-mensal/Dockerfile \
#                -t local/relatorio-outorgas-mensal .
#
# Sem ela, `EXECUTOR_BACKEND=docker` nao tem o que rodar, e o botao Executar
# falha logo depois de um `make up` limpo.
#
# A imagem e deliberadamente minima: a automacao recebe os parametros por
# arquivo, escreve os artefatos no diretorio que o runner informa em
# ORBITA_DIR_TRABALHO, e fala com a plataforma pelo stdout. Nao precisa de
# rede, de credencial nem de acesso ao banco.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /automacao

# O SDK e *instalado*, nao copiado para dentro do diretorio da automacao, e a
# diferenca importa por causa de um detalhe do interpretador.
#
# O manifesto declara `entrypoint: [python, -I, main.py]`. O `-I` roda em modo
# isolado, o que e desejavel em codigo de terceiro: ignora PYTHONPATH, ignora
# o site de usuario, e nao herda configuracao do ambiente. Mas `-I` tambem
# implica `-P`, que **nao acrescenta o diretorio do script ao sys.path**.
#
# Ou seja: com `-I`, um `hub_sdk/` ao lado de `main.py` nao e encontrado, e
# PYTHONPATH tambem nao resolve, porque `-I` o ignora. A unica forma que
# funciona e o SDK estar em site-packages, instalado.
COPY sdk /tmp/sdk
RUN pip install --no-cache-dir /tmp/sdk "openpyxl>=3.1" \
 && rm -rf /tmp/sdk

# Usuario sem privilegio. O runner tambem forca `user: 1000:1000` ao criar o
# container, entao isto e redundancia proposital: a imagem nao depende de o
# orquestrador fazer a coisa certa.
RUN useradd --create-home --uid 1000 automacao

COPY --chown=automacao:automacao automations/relatorio-outorgas-mensal/main.py /automacao/
COPY --chown=automacao:automacao automations/relatorio-outorgas-mensal/dados /automacao/dados

USER automacao

# O runner sobrescreve com o `entrypoint` do manifesto. Este CMD existe para
# que a imagem seja executavel sozinha, o que facilita depurar a automacao
# fora da plataforma:
#
#   docker run --rm -e ORBITA_DIR_TRABALHO=/tmp local/relatorio-outorgas-mensal
CMD ["python", "-I", "main.py"]
