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
	@bash docker/utils/lenny.sh --rebuild-reader

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

# Build and start containers (uses cache)
.PHONY: build
build:
	@bash docker/utils/lenny.sh --build

# Rebuild and start containers (recreate with no cache)
.PHONY: rebuild
rebuild:
	@bash docker/utils/lenny.sh --rebuild

.PHONY: stop
stop:
	@bash docker/utils/lenny.sh --stop
	@$(MAKE) untunnel

.PHONY: oauth-register
oauth-register:
	@bash docker/utils/register_client.sh


.PHONY: addbook
addbook:
	@if [ -z "$(olid)" ] || [ -z "$(filepath)" ]; then \
		echo "Error: Missing required arguments."; \
		echo "Usage: make addbook olid=OL123456M filepath=/path/to/book.epub [encrypted=true]"; \
		exit 1; \
	fi
	@bash docker/utils/addbook.sh --olid $(olid) --filepath $(filepath) $(if $(filter true,$(encrypted)),--encrypted,)


.PHONY: url
url:
	@TUNNEL_URL=$$(grep -aEo 'https://[a-zA-Z0-9.-]+\.(trycloudflare|cfargotunnel)\.com' cloudflared.log 2>/dev/null | head -n1); \
	if [ -z "$$TUNNEL_URL" ]; then \
		echo "[!] No tunnel URL found. Run 'make tunnel' first."; \
		exit 1; \
	fi; \
	OPDS_URL="$$TUNNEL_URL/v1/api/opds"; \
	ENCODED_OPDS=$$(python3 -c "import urllib.parse; print(urllib.parse.quote('$$OPDS_URL', safe=''))"); \
	READER_URL="https://reader.archive.org/?opds=$$ENCODED_OPDS"; \
	echo "[+] OPDS Feed: $$OPDS_URL"; \
	echo "[+] Reader URL: $$READER_URL"