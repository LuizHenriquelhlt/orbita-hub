# 0008 - Requisitos de publicação verificáveis, e os que não são

**Situação:** aceita · **Data:** 2026-10-17
**Implementa:** `hub_catalog/ciclo_de_vida.py`

## Contexto

Publicar uma automação a torna visível e executável para a organização. Uma
porta de qualidade antes disso é o que separa um catálogo útil de um depósito
de scripts sem dono.

A arquitetura-alvo original exigia, para publicar: *scan* de vulnerabilidade
da imagem sem críticos abertos, README preenchido, um responsável técnico e um
aprovador de negócio.

## Decisão

A função `requisitos_para_publicar` verifica o que é verificável **sem
infraestrutura externa** e devolve a **lista do que falta**, não um booleano.

Verificado: descrição com ao menos 40 caracteres, documentação, responsável
técnico, responsável de negócio, imagem declarada, ao menos uma versão
registrada, limite de concorrência válido.

**Não verificado, e dito em voz alta:** o scan de vulnerabilidade da imagem.
Ele exige um registry com scanner acoplado, que não existe no ambiente do
projeto. No lugar, o pipeline de CI roda a imagem das automações de exemplo.

A lista, e não o booleano, é a parte que importa na prática. "Não é possível
publicar" faz a pessoa abrir um chamado; "falta README e um responsável
técnico" ela resolve sozinha. A interface vai além: o botão de publicar
**some** quando há pendências, com a lista ao lado, um botão que sempre
devolve erro é pior do que botão ausente com a explicação.

## Consequências

**A favor.** A porta de qualidade existe e é acionável. A ausência do scan
está documentada em vez de implícita.

**Contra.** A verificação é de forma, não de conteúdo: nada impede uma
descrição de 40 caracteres sem sentido. É um limite conhecido, automatizar
julgamento de qualidade de texto trocaria um problema por outro pior.

E a aprovação por um segundo par de olhos é garantida apenas pela permissão:
quem envia para homologação precisa de `automacao:editar`, quem publica
precisa de `automacao:publicar`. Nada impede que a mesma pessoa tenha as duas
- a segregação é organizacional, não técnica.

## Alternativas

- **Exigir o scan e bloquear sem ele.** Tornaria impossível publicar no
  ambiente do projeto, e a reação natural seria desligar a verificação, o que
  é pior do que não tê-la.
- **Checklist manual marcado por quem publica.** Converte a verificação em
  declaração, que é o que se queria evitar.
- **Publicar sem requisitos e cobrar depois.** É o estado inicial de todo
  catálogo que vira depósito.
