<#
.SYNOPSIS
    Causiq developer commands for Windows. The PowerShell equivalent of the Makefile.

.DESCRIPTION
    `make` is not present on a default Windows install, and AC-17 requires a new
    engineer to reach a green suite on Windows as well as Linux. This script runs
    exactly the same commands as the Makefile targets so the two cannot drift in
    behaviour, only in syntax.

.EXAMPLE
    ./tasks.ps1 setup
    ./tasks.ps1 check
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('help', 'setup', 'lint', 'format', 'type', 'test', 'cov', 'test-live', 'check', 'schemas', 'clean')]
    [string]$Target = 'help'
)

$ErrorActionPreference = 'Stop'

# --no-sync: the OneDrive-synced working copy intermittently locks the
# dist-info directory uv wants to replace on each run. `setup` does the sync.
function Invoke-Uv {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    & uv run --no-sync @Arguments
    if ($LASTEXITCODE -ne 0) { throw "failed: uv run --no-sync $($Arguments -join ' ')" }
}

function Invoke-Lint {
    Invoke-Uv ruff check src tests scripts
    Invoke-Uv ruff format --check src tests scripts
}

function Invoke-Cov {
    # Gate 1: >= 85% overall.
    Invoke-Uv pytest -m 'not live' --cov --cov-report=term-missing --cov-fail-under=85
    # Gate 2: 100% on the invariant-critical modules.
    Invoke-Uv pytest -m 'not live' -q --cov=src/causiq/domain --cov=src/causiq/evidence `
        --cov=src/causiq/authz --cov=src/causiq/tools --cov-report=term-missing --cov-fail-under=100
}

switch ($Target) {
    'help' {
        Write-Output @'
setup      - create the virtualenv and install dependencies
lint       - ruff check + format check
format     - ruff format (writes)
type       - mypy --strict
test       - offline test suite (no API key, no network)
cov        - offline suite with both coverage gates
test-live  - live-API suite (requires ANTHROPIC_API_KEY)
check      - lint + type + cov  (the CI gate)
schemas    - regenerate golden JSON schemas (intended changes only)
clean      - remove caches and run artifacts
'@
    }
    'setup'     { & uv sync --extra dev; if ($LASTEXITCODE -ne 0) { throw 'uv sync failed' } }
    'lint'      { Invoke-Lint }
    'format'    { Invoke-Uv ruff format src tests scripts }
    'type'      { Invoke-Uv mypy }
    'test'      { Invoke-Uv pytest -m 'not live' }
    'cov'       { Invoke-Cov }
    'test-live' { Invoke-Uv pytest -m live }
    'check'     { Invoke-Lint; Invoke-Uv mypy; Invoke-Cov }
    'schemas'   { Invoke-Uv python scripts/regenerate_golden_schemas.py }
    'clean' {
        foreach ($path in '.mypy_cache', '.ruff_cache', '.coverage', 'htmlcov', 'var') {
            if (Test-Path $path) { Remove-Item -Recurse -Force $path }
        }
    }
}
