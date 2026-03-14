# Database Migrations

Lenny uses [Alembic](https://alembic.sqlalchemy.org/) to manage PostgreSQL schema changes. Migrations live in `alembic/versions/` and are applied automatically on every container start.

---

## For Instance Operators

### Normal Updates

Migrations apply automatically. When you pull a new version:

```bash
git pull
make redeploy
```

`make redeploy` rebuilds only the API image and restarts it — your database and all data are preserved. Migrations run automatically on startup via `alembic upgrade head`. Already-applied migrations are skipped. If you're updating across multiple versions, all pending migrations are applied sequentially in order.

**Expected downtime:** `make redeploy` causes ~15-30 seconds of downtime while the API container restarts and migrations run. This is normal. For a brief maintenance window, notify users before running the update.

> ⚠️ **Do not use `make build`, `make rebuild`, or `make resetdb` for updates** — these wipe the database volume and all your data. They are only for fresh installs or intentional resets.

### Checking Migration Status

```bash
make migrate-status
```

Shows the current revision and full migration history inside the container.

### Upgrading from a Pre-Alembic Instance

If you have an existing Lenny database from before Alembic was introduced, **it is handled automatically**. On first redeploy, the baseline migration detects your existing tables and skips creation — only registering itself in `alembic_version`. No data is touched, no manual steps needed.

### Rolling Back a Migration

For emergencies only. Rolls back the last applied migration:

```bash
make migrate-rollback
```

You will get an interactive confirmation prompt before anything runs.

> ⚠️ **Back up your database before rolling back destructive migrations** (DROP COLUMN, DROP TABLE). A rollback recreates the schema but cannot recover data that was in a dropped column or table.

### What Happens on Failure

If a migration fails, the container won't start. PostgreSQL rolls back the transaction, so the database remains unchanged. Check the logs:

```bash
make log
```

Fix the issue, then restart. Common causes:
- Migration file references a deleted revision (see [Deleted Migration Files](#deleted-migration-files) below)
- Manual schema changes that conflict with the migration

---

## For Developers

### Requirements

The container must be running to generate migrations:

```bash
make start       # ensure containers are up
make migration msg="describe your change"
```

Alembic connects to the live DB to compare models against the actual schema.

### Workflow

1. Modify `lenny/core/models.py` with your schema changes
2. Generate a migration:

```bash
make migration msg="add pkce authorization codes table"
```

3. Review the generated file in `alembic/versions/` — autogenerate is good but not perfect (see below)
4. Commit **both** the model changes and the migration file together

### Migration Files Must Be Committed

The files in `alembic/versions/` are what Alembic uses to determine what to apply. When users do `git pull && make redeploy`, these files tell Alembic what schema changes to run. **Never delete a migration file that has been pushed** unless you know what you're doing (see [Deleted Migration Files](#deleted-migration-files)).

### What Autogenerate Handles

Alembic's `--autogenerate` detects:

- New and dropped tables
- Added and removed columns
- Changed column types, nullability, server defaults
- Added and dropped indexes / unique constraints
- Added and dropped foreign keys

### What Needs Manual Editing

Autogenerate **cannot** detect:

- **Renamed columns** — shows up as drop + add, which destroys data. Edit the migration to use `op.alter_column(..., new_column_name=...)` instead.
- **Data migrations** — populating new columns from existing data. Write these by hand in `upgrade()`.
- **Enum value additions** — PostgreSQL enums require explicit `ALTER TYPE ... ADD VALUE` statements.

Always review the generated file before committing.

### Before Destructive Migrations

If your migration drops a column or table, document it clearly in the release notes so instance operators know to take a backup first:

```
⚠️ This release drops the `old_column` column from `items`. Back up your database before upgrading.
```

### DB URL Security

`alembic.ini` contains a placeholder URL only — safe to commit:

```ini
sqlalchemy.url = postgresql+psycopg2://user:pass@localhost/dbname
```

The real database URL is loaded from environment variables via `alembic/env.py`. Never put real credentials in `alembic.ini`. The `.env` file is gitignored.

### Squashing Migrations

On major releases, squash all migrations into a fresh baseline to prevent file accumulation:

```bash
make squash-migrations
```

This deletes all existing migration files and generates a single new baseline from the current model state.

> ⚠️ **After squashing, existing instances must re-stamp their database:**
> ```bash
> make migrate-stamp
> ```
> Without this, their `alembic_version` points to revisions that no longer exist and Alembic will error on next restart. Always announce squashes in release notes.

---

## Edge Cases

| Scenario | What Happens |
|---|---|
| Container restart with no pending migrations | `alembic upgrade head` runs, detects nothing to do, skips silently. |
| Container restart with pending migrations | Migrations apply automatically before the app starts. |
| Pre-Alembic existing database | Baseline migration detects existing tables and skips creation — just stamps the revision. No data touched. |
| Migration failure | Container won't start. PostgreSQL rolls back the transaction. Database stays unchanged. |
| Skipping multiple versions | `alembic upgrade head` applies all pending migrations sequentially, oldest first. |
| Multiple workers | Alembic runs once before `uvicorn` forks workers. No race condition. |
| `make build` / `make rebuild` / `make resetdb` | Wipes the database volume entirely. All data lost. Only use for fresh installs. |
| Deleted migration file (pushed revision) | Alembic errors: "Can't locate revision". See below. |
| After `make squash-migrations` | Existing instances must run `make migrate-stamp` or Alembic will error on restart. |

### Deleted Migration Files

If a migration file is deleted from the repo but the DB already has that revision in `alembic_version`, Alembic will error on startup:

```
alembic.util.exc.CommandError: Can't locate revision identified by '...'
```

Fix by stamping the DB to the nearest valid revision:

```bash
make migrate-status          # find the current broken revision
make migrate-stamp           # stamp to head (if DB schema is already correct)
```

If Alembic itself errors when stamping (because it validates the current broken revision first), bypass it directly in the DB:

```bash
docker exec lenny_db psql -U $DB_USER -d $DB_NAME \
  -c "UPDATE alembic_version SET version_num = '001_baseline'"
```

Replace `001_baseline` with the last known good revision ID.

---

## Commands Reference

| Task | Command | Data Safe? |
|---|---|---|
| Update instance (normal) | `make redeploy` | ✅ Yes |
| Apply pending migrations manually | `make migrate` | ✅ Yes |
| Check migration status | `make migrate-status` | ✅ Yes |
| Generate new migration | `make migration msg="description"` | ✅ Yes |
| Rollback last migration | `make migrate-rollback` | ⚠️ Schema only |
| Stamp existing DB | `make migrate-stamp` | ✅ Yes |
| Squash all migrations | `make squash-migrations` | ✅ Yes (but notify users) |
| Fresh install / reset | `make rebuild` | ❌ Wipes DB |
| Reset database only | `make resetdb` | ❌ Wipes DB |
