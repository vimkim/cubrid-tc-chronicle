set positional-arguments

# List available commands.
default:
    @just --list

# Install the locked environment; accepts uv sync options.
setup *args:
    uv sync --locked "$@"

# Reinstall the user-level tcc command from the primary main checkout (daily-update).
sync:
    @test -d .git || { echo "Run just sync from the primary checkout, not a task worktree." >&2; exit 1; }
    @test "$(git branch --show-current)" = main || { echo "Run just sync from main." >&2; exit 1; }
    uv tool install --editable . --reinstall
    @"$(uv tool dir --bin)/tcc" --version

# Run any CLI command, including global options such as --data-dir.
tcc +args:
    uv run --locked tcc "$@"

# Serve the web UI on all LAN interfaces and print access URLs; accepts --port.
serve *args:
    uv run --locked tcc serve "$@"

# Serve the web UI only on loopback; accepts --port.
serve-local *args:
    uv run --locked tcc serve --local "$@"

# Collect recent history, selected runs, or a bounded UTC window.
sync-data *args:
    uv run --locked tcc sync-data "$@"

# Summarize recent verdicts, suspected flakiness, freshness, and evidence for a TC.
status case *args:
    uv run --locked tcc status "$@"

# Find testcase identities by ticket, filename, or path.
find term *args:
    uv run --locked tcc find "$@"

# Query retained history for a testcase; accepts history filters.
history case *args:
    uv run --locked tcc history "$@"

# Explain a selected failure; requires --run or --execution and --case.
explain +args:
    uv run --locked tcc explain "$@"

# Inspect discovery, missing results, and provenance gaps.
coverage *args:
    uv run --locked tcc coverage "$@"

# Read retained evidence; requires --execution.
evidence +args:
    uv run --locked tcc evidence "$@"

# Verify and reparse retained evidence without network access.
reparse *args:
    uv run --locked tcc reparse "$@"

# Parse a local evidence bundle without changing the index.
inspect bundle *args:
    uv run --locked tcc inspect "$@"

# Preview a document with docserve; defaults to README.md.
docs path="README.md" *args:
    docserve "$@"

# Check Python lint and formatting.
lint:
    uv run --locked ruff check src tests scripts
    uv run --locked ruff format --check src tests scripts

# Run Python tests; accepts unittest discovery options.
test *args:
    uv run --locked python -m unittest discover -s tests "$@"

# Run Python lint, formatting, and tests.
check: lint test

# Build the source distribution and wheel.
build *args:
    uv build "$@"

# Install optional browser dependencies and Chromium for headless checks.
browser-setup:
    uv sync --locked --group browser
    uv run --locked --group browser playwright install chromium

# Verify the web UI against retained history with headless Chromium.
browser-check *args:
    uv run --locked --group browser python scripts/browser_check.py "$@"
