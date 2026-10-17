# 0001 - Registrar decisões, e registrá-las curtas

**Situação:** aceita · **Data:** 2026-10-17

## Contexto

Este repositório existe para mostrar como se decide, não só o que foi
construído. Código mostra a decisão final; não mostra as alternativas
consideradas nem o que se perdeu ao escolher.

A forma tradicional, um documento de arquitetura, envelhece sem ninguém
perceber, porque não está onde o código muda.

## Decisão

Toda decisão que alguém poderia razoavelmente ter tomado de outro jeito vira
um ADR curto aqui, **referenciado por comentário no ponto do código que a
implementa**. A referência cruzada é o que mantém as duas pontas vivas: quem
mexe no código vê o número da ADR; quem lê a ADR sabe onde ela virou código.

Formato fixo e enxuto:

- **Contexto**, qual pressão existia;
- **Decisão**, o que foi feito;
- **Consequências**, o que se ganhou e, principalmente, **o que se perdeu**;
- **Alternativas**, o que foi considerado e por que não.

A seção de consequências negativas não é opcional. Uma decisão sem custo
declarado é uma decisão que ninguém examinou de verdade.

## Consequências

**A favor.** O "por quê" fica junto do "o quê". Revisão de código pode
discordar de uma decisão citando a ADR.

**Contra.** ADR desatualizada é pior do que ADR nenhuma, porque dá autoridade
a uma decisão revogada. A mitigação é mantê-las curtas: um documento de 40
linhas é barato de corrigir; um de 400, ninguém corrige.

## Alternativas

- **Documento único de arquitetura.** Foi o ponto de partida deste projeto, e
  é o que gerou a pergunta: qual parágrafo daquele documento ainda vale? Com
  ADRs numeradas, a resposta é verificável, uma ADR pode ser marcada como
  substituída por outra.
- **Só comentários no código.** Funciona para o "como", não para o "em vez
  de quê". A alternativa descartada não tem lugar natural no código que a
  descartou.
