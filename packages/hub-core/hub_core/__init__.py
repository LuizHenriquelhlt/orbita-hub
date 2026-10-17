"""Núcleo compartilhado da plataforma Órbita.

Este pacote contém tudo que os quatro serviços têm em comum, e nada que seja
específico de um domínio. A regra que mantém isso honesto: `hub_core` não
importa nenhum `hub_identity` / `hub_catalog` / `hub_execution` / `hub_runner`.
A dependência só aponta para dentro.

As quatro dependências de infraestrutura da plataforma (fila, executor,
armazenamento de artefatos e lock distribuído) são declaradas aqui como
*portas*, classes abstratas, com ao menos duas implementações cada: uma de
produção e uma que roda sem nenhum serviço externo. É isso que permite o mesmo
código rodar em cinco containers com RabbitMQ e em um único processo com
PostgreSQL. Ver docs/adr/0002-adaptadores-plugaveis.md.
"""

__version__ = "0.1.0"
