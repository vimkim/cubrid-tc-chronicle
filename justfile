set positional-arguments

# List available commands.
default:
    @just --list

# Install the locked environment; accepts uv sync options.
setup *args:
    uv sync --locked "$@"

# Install the user-level cubrid-tcc CLI and AI agent skill from the primary main checkout.
install: sync

# Reinstall the user-level cubrid-tcc command and skill from the primary main checkout (daily-update).
sync:
    @test -d .git || { echo "Run just sync from the primary checkout, not a task worktree." >&2; exit 1; }
    @test "$(git branch --show-current)" = main || { echo "Run just sync from main." >&2; exit 1; }
    uv tool install --reinstall .
    @"$(uv tool dir --bin)/cubrid-tcc" --version
    "{{just_executable()}}" install-skills

# Install bundled skills (copy to ~/.agents/skills and link for Claude, Codex, and Antigravity).
install-skills:
    #!/usr/bin/env bash
    set -euo pipefail
    skill_root="{{justfile_directory()}}/skills"
    agents_dir="$HOME/.agents/skills"
    claude_dir="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills"
    codex_dir="${CODEX_HOME:-$HOME/.codex}/skills"
    agy_dir="$HOME/.gemini/config/skills"
    mkdir -p "$agents_dir" "$claude_dir" "$codex_dir" "$agy_dir"

    link_agent() {
        local target="$1"
        local dir="$2"
        local name="$3"
        local link_path="$dir/$name"
        if [ -L "$link_path" ] || [ -e "$link_path" ]; then
            rm -rf "${dir:?}/${name:?}"
        fi
        ln -s "$target" "$link_path"
    }

    for src in "$skill_root"/*; do
        [ -d "$src" ] && [ -f "$src/SKILL.md" ] || continue
        name="$(basename "$src")"
        staging="$(mktemp -d "${agents_dir}/.${name}.XXXXXX")"
        cp -R "$src/." "$staging/"
        find "$staging" -type f -name '*.pyc' -delete
        find "$staging" -type d -name '__pycache__' -empty -delete
        if [ -L "$agents_dir/$name" ] || [ -e "$agents_dir/$name" ]; then
            rm -rf "${agents_dir:?}/${name:?}"
        fi
        mv "$staging" "$agents_dir/$name"
        if [ "$claude_dir" = "$HOME/.claude/skills" ]; then
            link_agent "../../.agents/skills/$name" "$claude_dir" "$name"
        else
            link_agent "$agents_dir/$name" "$claude_dir" "$name"
        fi
        link_agent "$agents_dir/$name" "$codex_dir" "$name"
        link_agent "$agents_dir/$name" "$agy_dir" "$name"
        printf 'Installed skill %s -> %s\n' "$name" "$agents_dir/$name"
    done

# Run any CLI command, including global options such as --data-dir.
cubrid-tcc +args:
    uv run --locked cubrid-tcc "$@"

# Serve the web UI on all LAN interfaces and print access URLs; accepts --port.
serve *args:
    uv run --locked cubrid-tcc serve "$@"

# Serve the web UI only on loopback; accepts --port.
serve-local *args:
    uv run --locked cubrid-tcc serve --local "$@"

# Collect recent history, selected runs, or a bounded UTC window.
sync-data *args:
    uv run --locked cubrid-tcc sync-data "$@"

# Install the hourly systemd user timer and its service without enabling them.
install-timer:
    #!/usr/bin/env bash
    set -euo pipefail
    command -v cubrid-tcc >/dev/null || { echo "Run just sync and add cubrid-tcc to PATH." >&2; exit 1; }
    command -v gh >/dev/null || { echo "Install the authenticated gh CLI and add it to PATH." >&2; exit 1; }
    unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
    for unit in cubrid-tcc-sync.service cubrid-tcc-sync.timer; do
        target="$unit_dir/$unit"
        if [[ -L "$target" ]] || { [[ -e "$target" ]] && [[ "$(head -n 1 "$target")" != '# Managed by cubrid-tc-chronicle' ]]; }; then
            echo "Refusing to replace unmanaged unit $target" >&2
            exit 1
        fi
    done
    service_path=${PATH//\\/\\\\}
    service_path=${service_path//\"/\\\"}
    service_path=${service_path//%/%%}
    service=$(<systemd/cubrid-tcc-sync.service)
    printf '%s\n' "${service//@PATH@/"$service_path"}" | install -Dm644 /dev/stdin "$unit_dir/cubrid-tcc-sync.service"
    install -Dm644 systemd/cubrid-tcc-sync.timer "$unit_dir/cubrid-tcc-sync.timer"
    systemctl --user daemon-reload
    echo "Timer installed. To enable and start it, run systemctl --user enable --now cubrid-tcc-sync.timer."

# Show timer registration, scheduling, and state.
timer-status:
    @systemctl --user show --property=LoadState,UnitFileState,ActiveState,NextElapseUSecRealtime cubrid-tcc-sync.timer

# Show recent collection logs.
timer-logs:
    @journalctl --user -u cubrid-tcc-sync.service -n 50 --no-pager

# Summarize recent verdicts, suspected flakiness, freshness, and evidence for a TC.
status case *args:
    uv run --locked cubrid-tcc status "$@"

# Find testcase identities by ticket, filename, or path.
find term *args:
    uv run --locked cubrid-tcc find "$@"

# Query retained history for a testcase; accepts history filters.
history case *args:
    uv run --locked cubrid-tcc history "$@"

# Explain a selected failure; requires --run or --execution and --case.
explain +args:
    uv run --locked cubrid-tcc explain "$@"

# Inspect discovery, missing results, and provenance gaps.
coverage *args:
    uv run --locked cubrid-tcc coverage "$@"

# Read retained evidence; requires --execution.
evidence +args:
    uv run --locked cubrid-tcc evidence "$@"

# Verify and reparse retained evidence without network access.
reparse *args:
    uv run --locked cubrid-tcc reparse "$@"

# Parse a local evidence bundle without changing the index.
inspect bundle *args:
    uv run --locked cubrid-tcc inspect "$@"

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
