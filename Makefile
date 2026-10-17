# Órbita, atalhos de desenvolvimento.
# `make ajuda` lista tudo.

COMPOSE := docker compose -f infra/docker-compose.yml --env-file .env

.DEFAULT_GOAL := ajuda
.PHONY: ajuda up down logs clean seed migrar teste lint fmt tipos mono

ajuda:  ## Lista os alvos disponíveis
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	 | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

up:  ## Sobe o ambiente distribuído completo (5 serviços + pg + rabbit + redis + minio)
	@test -f .env || cp .env.example .env
	$(COMPOSE) up -d --build

down:  ## Para e remove os containers
	$(COMPOSE) down

clean:  ## Remove containers E volumes (apaga o banco local)
	$(COMPOSE) down -v

logs:  ## Acompanha os logs de todos os serviços
	$(COMPOSE) logs -f

migrar:  ## Aplica as migrações do Alembic em todos os schemas
	$(COMPOSE) run --rm identity alembic upgrade head

seed:  ## Popula o banco com o cenário fictício do INRH
	$(COMPOSE) run --rm identity python -m scripts.seed

mono:  ## Roda tudo em UM processo (igual ao free tier), sem Docker
	HUB_MODE=monolith BROKER_BACKEND=postgres EXECUTOR_BACKEND=simulated \
	STORAGE_BACKEND=postgres LOCK_BACKEND=postgres \
	uvicorn hub_gateway.main:app --reload --port 8000

teste:  ## Roda a suíte de testes
	pytest

lint:  ## Checa estilo e erros estáticos
	ruff check .
	ruff format --check .

fmt:  ## Formata o código
	ruff format .
	ruff check --fix .

tipos:  ## Checagem de tipos
	mypy packages services gateway sdk
