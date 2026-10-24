# Runs `alembic upgrade head` against production (PROD_DATABASE_URL) with guards.
# Dry run by default (shows the stamp and the pending revisions); -Apply upgrades.
# Never prints the connection string. Run from the repo root:
#   powershell -File scripts/prod_alembic_upgrade.ps1            # dry run
#   powershell -File scripts/prod_alembic_upgrade.ps1 -Apply     # upgrade
param([switch]$Apply)
$ErrorActionPreference = "Stop"
$py = ".\.venv-api\Scripts\python.exe"

$url = $env:PROD_DATABASE_URL
if (-not $url) { $url = [Environment]::GetEnvironmentVariable("PROD_DATABASE_URL", "User") }
if (-not $url) { throw "PROD_DATABASE_URL is not set." }
$env:DATABASE_URL = $url   # this process only; a bare alembic elsewhere never sees it
Remove-Item Env:POSTGRES_URL -ErrorAction SilentlyContinue

$headLine = & $py -m alembic heads
if (@($headLine).Count -ne 1) { throw "Expected a single Alembic head, got: $headLine" }
$head = ($headLine -split " ")[0]
$parent = ((& $py -m alembic show head) | Select-String "^Parent:").Line -replace "Parent:\s*", ""
$current = ((& $py -m alembic current) | Where-Object { $_ -match "^[0-9a-f]{12}" } | Select-Object -First 1)
$current = if ($current) { ($current -split " ")[0] } else { "" }

"head:    $head"
"parent:  $parent"
"current: $current"
if ($current -eq $head) { "Already at head. Nothing to do."; exit 0 }
if ($current -ne $parent) {
    throw "Production stamp ($current) is not the revision just before head ($parent). Stopping: more than one migration would run, or the stamp is off. Check by hand."
}
if (-not $Apply) { "Dry run: would apply $head. Re-run with -Apply after taking a Neon snapshot."; exit 0 }

& $py -m alembic upgrade head
"now: " + ((& $py -m alembic current) | Where-Object { $_ -match "^[0-9a-f]{12}" })
