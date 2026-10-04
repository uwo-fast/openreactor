set dotenv-load := false

# Show available recipes
default:
    @just --list

# Install the locked dependencies (run `uv lock` after editing pyproject.toml)
setup:
    uv sync --locked

# Format code (changes the working tree)
fmt:
    uv run ruff format
    uv run ruff check --fix

# Verify formatting without changing anything
fmt-check:
    uv run ruff format --check

# Run linters
lint:
    uv run ruff check

# Type-check
typecheck:
    uv run pyright

# Format check, lint and type-check, as CI runs them
check: fmt-check lint typecheck

# Run tests
test:
    uv run pytest

# Run openreactor, for example: just run check-config examples/openreactor.toml
run *args:
    uv run openreactor {{args}}

# Download the browser files in static/vendor/vendor.toml and record their hashes
vendor:
    uv run python scripts/vendor.py

# Build the sdist and wheel into dist/
build:
    uv build

# Remove the environment and build and tool caches
clean:
    rm -rf .venv dist .pytest_cache .ruff_cache
    find src tests -type d -name __pycache__ -prune -exec rm -rf {} +
