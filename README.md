# Compendium

A library card catalog system for physical items.

**Status:** Early development. See [`CLAUDE.md`](CLAUDE.md) for the full design and architecture document.

## Quick start

```bash
# Install dependencies into a project-local virtualenv
uv sync --extra dev

# Initialise the database (creates ./compendium.db on SQLite by default)
uv run compendium db init

# Add a book by ISBN (looks up metadata from Open Library)
uv run compendium item add --isbn 9780441013593

# Add a patron
uv run compendium patron add --name "A. Patron"

# Check it out (use the barcode reported by `item add` and the card number from `patron add`)
uv run compendium loan checkout --barcode <barcode> --card <card>

# Check it back in
uv run compendium loan checkin --barcode <barcode>
```

## Layout

- `src/compendium/` — application code (see `CLAUDE.md` for layer breakdown).
- `migrations/` — Alembic migrations.
- `tests/` — unit and integration tests.

## License

MIT (assumed; finalize before first release).
