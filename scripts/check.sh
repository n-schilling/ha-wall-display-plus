#!/bin/bash
# Runs locally what the CI workflow runs, so a push only confirms what already
# passed here: ruff, the changelog and translation checks, mypy, the tests with
# their coverage threshold, and hassfest in the very container the GitHub
# action starts. Keep the steps in step with .github/workflows/ci.yaml.
#
# Usage: scripts/check.sh [--no-hassfest]
#
# The CI job HACS is left to GitHub: its action reads the repository from
# GitHub, not from this checkout.
#
# The tools come from .venv when there is one, otherwise from PATH (a
# devcontainer, another virtual environment, a system installation).
#
# Hassfest needs Docker. Its image exists for amd64 only; on Apple silicon
# Docker runs it emulated, which takes about 20 seconds, and on an ARM Linux
# without emulation the step fails, so pass --no-hassfest there and let CI
# run it. The image is pulled once and then reused; `docker pull
# ghcr.io/home-assistant/hassfest` refreshes it, and the weekly CI run catches
# a Home Assistant change in the meantime.
set -uo pipefail
cd "$(dirname "$0")/.."

tool() { # tool <name> [<alternative>]: the one in .venv, else the one on PATH
    local name
    for name in "$@"; do
        if [[ -x ".venv/bin/$name" ]]; then
            echo ".venv/bin/$name"
            return
        fi
    done
    for name in "$@"; do
        if command -v "$name"; then
            return
        fi
    done
    echo "$1 not found, neither in .venv nor on PATH; see README, section Development." >&2
    return 1
}

RUFF=$(tool ruff) || exit 2
MYPY=$(tool mypy) || exit 2
PYTEST=$(tool pytest) || exit 2
PYTHON=$(tool python python3) || exit 2

failed=()
skipped=()

run() { # run <name> <command...>
    local name=$1
    shift
    echo "==> $name"
    if "$@"; then
        echo "    ok: $name"
    else
        echo "    FAILED: $name"
        failed+=("$name")
    fi
    echo
}

version=$("$PYTHON" -c 'import json, sys; print(json.load(open(sys.argv[1]))["version"])' \
    custom_components/wall_display_plus/manifest.json)

run "Ruff check" "$RUFF" check .
run "Ruff format" "$RUFF" format --check .
run "Changelog has the version $version" bash -c \
    '.github/scripts/changelog-section.sh CHANGELOG.md "$1" > /dev/null' _ "$version"
run "Translations match strings.json" \
    diff custom_components/wall_display_plus/strings.json custom_components/wall_display_plus/translations/en.json
run "Strict typing" "$MYPY" custom_components
run "Tests" "$PYTEST" -q \
    --cov=custom_components.wall_display_plus --cov-report=term-missing:skip-covered --cov-fail-under=99

no_hassfest=false
for arg in "$@"; do
    case "$arg" in
        --no-hassfest) no_hassfest=true ;;
        *) echo "unknown option $arg" >&2; exit 2 ;;
    esac
done

if $no_hassfest; then
    skipped+=("Hassfest (--no-hassfest)")
elif ! docker info > /dev/null 2>&1; then
    skipped+=("Hassfest (Docker is not running)")
else
    # The double slash keeps Git Bash on Windows from rewriting the container path, as in
    # the action itself; the z label lets the container read the folder under SELinux
    run "Hassfest" docker run --rm --platform linux/amd64 \
        -v "$PWD"://github/workspace:ro,z ghcr.io/home-assistant/hassfest
fi

echo "===================="
# The ${arr[@]+"${arr[@]}"} form keeps an empty array from tripping `set -u` in the bash 3.2 of macOS
for name in ${skipped[@]+"${skipped[@]}"}; do
    echo "skipped: $name"
done
if ((${#failed[@]})); then
    echo "FAILED: ${failed[*]}"
    exit 1
fi
echo "All checks passed."
