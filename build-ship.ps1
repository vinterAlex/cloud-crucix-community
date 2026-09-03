param(
    # Escape hatch for the dist\secrets\ credential check below. Off by default
    # on purpose: shipping a key is not a mistake worth making quietly.
    [switch] $AllowKeysInDist
)

$ErrorActionPreference = "Stop"

# Build the hand-off artifact for Cloud Crucix Community Edition.
#
# Produces  dist\cloud-crucix-community-<version>.tar  from `docker save`:
# a single file a colleague can run with no Python, no pip install and no
# source code. The dist\ folder ends up holding everything they need and
# nothing they don't:
#
#     dist\cloud-crucix-community-<version>.tar    the image
#     dist\RUN-ME.bat                               double-click to start
#     dist\RUN-ME.txt                               the manual steps
#     dist\PERMISSIONS.txt                          what to ask their cloud team for
#     dist\ABOUT.md                                 feature overview
#     dist\secrets\                                 where their own key goes
#     dist\output\                                  where exported reports land
#
# Zip dist\ and send it. Their service-account key never comes from us.

$Root = $PSScriptRoot
$Dist = Join-Path $Root "dist"

# docker writes its build progress to stderr. Under $ErrorActionPreference =
# "Stop" (and especially if the caller redirects stderr) PowerShell turns each
# of those lines into a terminating NativeCommandError, which would abort a
# perfectly good build. So call docker with errors non-terminating and judge it
# by its exit code, which is what actually says whether it worked.
function Invoke-Docker {
    param([Parameter(ValueFromRemainingArguments = $true)] $DockerArgs)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { & docker @DockerArgs } finally { $ErrorActionPreference = $prev }
}

# Version auto-increments per build, so every artifact is visibly distinct.
$VersionFile = Join-Path $Root "VERSION.txt"
$Prev = if (Test-Path $VersionFile) { (Get-Content $VersionFile -Raw).Trim() } else { "1.0" }

# Community versioning: treat as major.minor (strip any suffix for increment).
$CleanPrev = $Prev -replace "-community$", ""
$Parts = $CleanPrev.Split(".")
$Minor = try { [int]$Parts[1] } catch { 0 }
$NewVersion = "$($Parts[0]).$($Minor + 1)"
$FullVersion = "$NewVersion-community"
$Image = "cloud-crucix-community:$FullVersion"
$OutTar = Join-Path $Dist "cloud-crucix-community-$FullVersion.tar"

Write-Host "=== Version: $Prev -> $FullVersion" -ForegroundColor Cyan

# ---------------------------------------------------------------------
Write-Host "=== Checking Docker ..." -ForegroundColor Cyan
Invoke-Docker version | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Docker is not running. Start Docker Desktop and retry." }

# A key left in the SOURCE secrets\ never reaches the image - .dockerignore
# excludes it - so this is only a note.
$strayKeys = Get-ChildItem (Join-Path $Root "secrets\*.json") -ErrorAction SilentlyContinue
if ($strayKeys) {
    Write-Host "    note: $($strayKeys.Count) key(s) in secrets\ - excluded from the image by .dockerignore" -ForegroundColor Yellow
}

# A key in dist\secrets\ is a different matter: dist\ is the folder that gets
# zipped and handed to someone else, so a key sitting there would be mailed
# along with the tool. Refuse to build rather than help that happen.
$shippedKeys = Get-ChildItem (Join-Path $Dist "secrets\*.json") -ErrorAction SilentlyContinue
if ($shippedKeys -and -not $AllowKeysInDist) {
    Write-Host ""
    Write-Host "  STOP: service-account key(s) found in dist\secrets\" -ForegroundColor Red
    foreach ($k in $shippedKeys) { Write-Host "         $($k.Name)" -ForegroundColor Red }
    Write-Host ""
    Write-Host "  dist\ is the folder you zip and send. Anyone who receives it would"
    Write-Host "  get these credentials. Move them to the source secrets\ folder,"
    Write-Host "  which is where your own instance reads from:"
    Write-Host ""
    Write-Host "      Move-Item dist\secrets\*.json secrets\"
    Write-Host ""
    Write-Host "  Then run this again. (-AllowKeysInDist overrides, if the recipient"
    Write-Host "  really is meant to have them.)"
    Write-Host ""
    throw "refusing to build with credentials in dist\secrets"
}

Write-Host "=== Building $Image ..." -ForegroundColor Cyan
Invoke-Docker build --tag $Image $Root
if ($LASTEXITCODE -ne 0) { throw "docker build failed" }

if (-not (Test-Path $Dist)) { New-Item -ItemType Directory -Path $Dist | Out-Null }
New-Item -ItemType Directory -Force -Path (Join-Path $Dist "secrets") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $Dist "output") | Out-Null

Write-Host "=== Dropping older tars from dist\ (only the latest ships) ..." -ForegroundColor Cyan
Get-ChildItem (Join-Path $Dist "cloud-crucix-community-*.tar") -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -ne (Split-Path $OutTar -Leaf) } |
    Remove-Item -Force

Write-Host "=== Saving image to $OutTar ..." -ForegroundColor Cyan
Invoke-Docker save --output $OutTar $Image
if ($LASTEXITCODE -ne 0) { throw "docker save failed" }

# Build + save succeeded: persist the new version.
[System.IO.File]::WriteAllText($VersionFile, $FullVersion,
    (New-Object System.Text.UTF8Encoding($false)))

Write-Host "=== Refreshing the hand-off docs ..." -ForegroundColor Cyan
Copy-Item (Join-Path $Root "PERMISSIONS.txt") (Join-Path $Dist "PERMISSIONS.txt") -Force
Copy-Item (Join-Path $Root "RUN-ME.bat") (Join-Path $Dist "RUN-ME.bat") -Force
Copy-Item (Join-Path $Root "RUN-ME.txt") (Join-Path $Dist "RUN-ME.txt") -Force
Copy-Item (Join-Path $Root "ABOUT.md") (Join-Path $Dist "ABOUT.md") -Force
Copy-Item (Join-Path $Root "secrets\README.txt") (Join-Path $Dist "secrets\README.txt") -Force

# Keep the version strings in the colleague-facing files in step with this build.
# Written back without a BOM: cmd.exe can choke on a BOM at the top of a .bat.
$NoBom = New-Object System.Text.UTF8Encoding($false)
foreach ($doc in @("RUN-ME.bat", "RUN-ME.txt")) {
    $path = Join-Path $Dist $doc
    if (Test-Path $path) {
        $text = (Get-Content -LiteralPath $path -Raw) `
            -replace "cloud-crucix-community-\d+\.\d+-community\.tar", "cloud-crucix-community-$FullVersion.tar" `
            -replace "cloud-crucix-community:\d+\.\d+-community", "cloud-crucix-community:$FullVersion" `
            -replace "cloud-crucix-community:local", "cloud-crucix-community:$FullVersion"
        [System.IO.File]::WriteAllText($path, $text, $NoBom)
    }
}

# Don't let previous builds pile up in Docker Desktop. The old tag may not
# exist locally, so this must not be able to fail the build.
if ($Prev -ne $FullVersion) {
    try {
        $ErrorActionPreference = "Continue"
        Invoke-Docker rmi "cloud-crucix-community:$Prev" *> $null
        Invoke-Docker rmi "cloud-crucix-community:$CleanPrev" *> $null
    } finally {
        $ErrorActionPreference = "Stop"
        $global:LASTEXITCODE = 0
    }
}

$Hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $OutTar).Hash
$SizeMb = [math]::Round((Get-Item $OutTar).Length / 1MB, 1)

Write-Host ""
Write-Host "=====================================================" -ForegroundColor Green
Write-Host "  ARTIFACT READY  (Community Edition)"
Write-Host "  File   : $OutTar"
Write-Host "  Size   : $SizeMb MB"
Write-Host "  SHA256 : $Hash"
Write-Host "=====================================================" -ForegroundColor Green
Write-Host ""
Write-Host "Send the whole dist\ folder (zip it). Your colleague drops their"
Write-Host "own service-account .json into dist\secrets\ and double-clicks"
Write-Host "RUN-ME.bat. No Python, no source code, no gcloud needed."
Write-Host ""

exit 0
