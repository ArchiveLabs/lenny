# Makefile for common lenny operations

container ?= lenny_api

# Default target
.PHONY: all
all: start

.PHONY: configure
configure:
	@bash docker/configure.sh

# Succeed if lenny is up, else fail
.PHONY: ifup
ifup:
	@docker ps -q -f name=$(container) > /dev/null || { \
		echo "[!] $(container) is not running. Aborting."; \
		exit 1; \
	}

# Preload books (pass an optional number to limit)
# e.g. make preload items=5
.PHONY: preload
preload: ifup
	@bash docker/utils/preload.sh $(items)

# Start a public tunnel (e.g., via cloudflared)
.PHONY: tunnel
tunnel:
	@bash docker/utils/tunnel.sh --start

# Start a public tunnel (e.g., via cloudflared)
.PHONY: untunnel
untunnel:
	@bash docker/utils/tunnel.sh --stop

# Teardown all containers, volumes, and orphans for a clean slate
.PHONY: teardown
teardown:
	docker compose down --volumes --remove-orphans

.PHONY: log
log:
	@docker compose logs -f

.PHONY: resetdb
resetdb:
	@docker compose -p lenny down -v

.PHONY: start
start:
	@bash docker/utils/lenny.sh --start

.PHONY: restart
restart:
	@bash docker/utils/lenny.sh --restart

# Rebuild and start containers (recreate with changes)
.PHONY: rebuild
rebuild:
	@bash docker/utils/lenny.sh --rebuild

.PHONY: stop
stop:
	@bash docker/utils/lenny.sh --stop
	@$(MAKE) untunnel
