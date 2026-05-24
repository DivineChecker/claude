COMPOSE = docker compose
IMAGE   = claude-incognito-bot

.PHONY: help build up down restart logs shell status clean rebuild dev health

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

build:    ## Build Docker image
	$(COMPOSE) build --no-cache

up:       ## Start bot (detached)
	$(COMPOSE) up -d
	@echo "✓ Bot started. Use 'make logs' to follow output."

down:     ## Stop bot
	$(COMPOSE) down

restart:  ## Restart bot
	$(COMPOSE) restart bot

logs:     ## Follow live logs
	$(COMPOSE) logs -f --tail=100 bot

shell:    ## Open shell inside container
	$(COMPOSE) exec bot sh

status:   ## Show container status + health
	$(COMPOSE) ps
	@echo ""
	$(COMPOSE) exec bot python healthcheck.py 2>/dev/null || true

clean:    ## Remove containers, images, volumes
	$(COMPOSE) down -v --rmi local
	docker image prune -f

rebuild:  ## Full clean rebuild + restart
	$(COMPOSE) down
	$(COMPOSE) build --no-cache
	$(COMPOSE) up -d

dev:      ## Start in dev mode (hot reload)
	$(COMPOSE) -f docker-compose.yml -f docker-compose.dev.yml up

health:   ## Run health check manually
	$(COMPOSE) exec bot python healthcheck.py

update:   ## Pull latest & rebuild
	git pull
	$(COMPOSE) build --no-cache
	$(COMPOSE) up -d
