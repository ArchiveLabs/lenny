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

# Remove the orphaned pre-Garage MinIO container + volume, once you've
# verified the migrated Garage bucket has everything (see `make update`).
# Opt-in, run manually — never done automatically.
.PHONY: cleanup-old-s3
cleanup-old-s3:
	@docker rm -f lenny_s3 lenny_s3_migration_source 2>/dev/null || true
	@docker volume rm lenny_s3_data 2>/dev/null || true
	@docker rmi minio/minio:latest minio/mc:latest 2>/dev/null || true
	@echo "Old MinIO container, volume, and images removed."

.PHONY: log
log:
	@docker compose logs -f

# WARNING: wipes database volume and all data.
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
# WARNING: wipes database volume. Use 'make redeploy' to preserve data.
.PHONY: build
build:
	@bash docker/utils/lenny.sh --build

# Rebuild and start containers (recreate with no cache)
# WARNING: wipes database volume. Use 'make redeploy' to preserve data.
.PHONY: rebuild
rebuild:
	@bash docker/utils/lenny.sh --rebuild

# Rebuild API image and apply migrations WITHOUT wiping data (safe for updates)
# Use this when pulling new code changes, migrations, or dependency updates.
.PHONY: redeploy
redeploy:
	@bash docker/utils/lenny.sh --redeploy

# ── OAuth 2.0 client administration ──────────────────────────────────────────
# Registration is open by default, so an operator needs to see who registered
# and be able to stop one without opening a Python console.
# usage: make oauth2-register NAME="Open Library" URI=https://openlibrary.org/lenny/callback
.PHONY: oauth2-register
oauth2-register:
	@test -n "$(NAME)" -a -n "$(URI)" || { echo 'usage: make oauth2-register NAME="Open Library" URI=https://…/callback [SCOPE=loans:read] [PUBLIC=1]'; exit 1; }
	@docker compose -p lenny exec api python3 scripts/oauth2_client.py register "$(NAME)" "$(URI)" $(if $(SCOPE),--scope $(SCOPE)) $(if $(PUBLIC),--public)

# Open Library is the consumer nearly every node wants, so it gets its own
# targets rather than an incantation. Both are safe to run twice.
# Optional: URI=https://…/callback overrides OL's callback; ROTATE=1 issues a
# new secret and retires the old registration along with its tokens.
.PHONY: ol-connect
ol-connect:
	@docker compose -p lenny exec api python3 scripts/oauth2_client.py ol-connect $(if $(URI),--redirect-uri $(URI)) $(if $(ROTATE),--rotate)

.PHONY: ol-disconnect
ol-disconnect:
	@docker compose -p lenny exec api python3 scripts/oauth2_client.py ol-disconnect

.PHONY: oauth2-clients
oauth2-clients:
	@docker compose -p lenny exec api python3 scripts/oauth2_client.py list

.PHONY: oauth2-disable
oauth2-disable:
	@test -n "$(CLIENT)" || { echo "usage: make oauth2-disable CLIENT=<client_id>"; exit 1; }
	@docker compose -p lenny exec api python3 scripts/oauth2_client.py disable $(CLIENT)

# Deletes codes and tokens that can no longer be used. Safe to run from cron.
.PHONY: oauth2-sweep
oauth2-sweep:
	@docker compose -p lenny exec api python3 scripts/oauth2_client.py sweep


.PHONY: stop
stop:
	@bash docker/utils/lenny.sh --stop
	@$(MAKE) untunnel


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

# Update to latest version
.PHONY: update
update:
	@bash docker/utils/update.sh

# Remove dangling images and cap build cache to free disk/memory.
# Safe: never touches running containers, named volumes, or DB data.
# Run manually after updates or whenever disk usage is high.
.PHONY: prune
prune:
	@echo "Pruning dangling images..."
	@docker image prune -f
	@echo "Capping build cache to 2 GB..."
	@docker builder prune -f --reserved-space=2gb
	@echo "Done."

# Log in to archive.org/openlibrary.org and store IA S3 keys in .env.
# Idempotent — safe to re-run. Use to log in, re-login with a different account,
# or recover from a failed lending setup.
.PHONY: ol-login
ol-login: ifup
	@bash docker/utils/ol_configure.sh

# Log out of archive.org — clears IA S3 keys from .env and disables lending.
.PHONY: ol-logout
ol-logout: ifup
	@bash docker/utils/ol_logout.sh

# Run environment diagnostics
.PHONY: doctor
doctor:
	@bash docker/utils/doctor.sh

# Database Migrations

# Run pending migrations inside container
.PHONY: migrate
migrate: ifup
	@docker exec $(container) alembic upgrade head

# Show current migration status
.PHONY: migrate-status
migrate-status: ifup
	@docker exec $(container) alembic current
	@docker exec $(container) alembic history

# Generate a new migration from model changes (developers only)
# Usage: make migration msg="add pkce tables"
.PHONY: migration
migration: ifup
	@if [ -z "$(msg)" ]; then \
		echo "Error: Missing migration message."; \
		echo "Usage: make migration msg=\"describe your changes\""; \
		exit 1; \
	fi
	@docker exec $(container) alembic revision --autogenerate -m "$(msg)"

# Rollback last migration (use with caution)
.PHONY: migrate-rollback
migrate-rollback: ifup
	@echo "WARNING: This will rollback the last migration."
	@echo "Press Ctrl+C to cancel, or Enter to continue..."
	@read _
	@docker exec $(container) alembic downgrade -1

# Stamp current DB as up-to-date (for existing databases adopting Alembic)
.PHONY: migrate-stamp
migrate-stamp: ifup
	@docker exec $(container) alembic stamp head
	@echo "Database stamped as up-to-date."

# Squash all migrations into a new baseline (major releases only)
# Usage: make squash-migrations
.PHONY: squash-migrations
squash-migrations: ifup
	@echo "WARNING: This will squash all migrations into a new baseline."
	@echo "Only do this on major releases. Press Ctrl+C to cancel, or Enter to continue..."
	@read _
	@rm -f alembic/versions/*.py
	@docker exec $(container) alembic revision --autogenerate -m "squashed baseline"
	@echo "New baseline created. Existing databases must run: make migrate-stamp"