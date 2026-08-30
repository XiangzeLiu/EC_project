param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("sm", "ts")]
    [string]$Target
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Write-Utf8NoBom {
    param([string]$Path, [string]$Content)
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Content, $encoding)
}

function Invoke-Native {
    param(
        [string]$FilePath,
        [string[]]$Arguments,
        [string]$FailureMessage
    )
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$FailureMessage (exit=$LASTEXITCODE)"
    }
}

function Invoke-GuiAndWait {
    param(
        [string]$FilePath,
        [string[]]$Arguments,
        [string]$WorkingDirectory,
        [string]$FailureMessage,
        [int]$TimeoutSeconds = 60
    )
    $process = Start-Process -FilePath $FilePath -ArgumentList $Arguments -WorkingDirectory $WorkingDirectory -PassThru
    try {
        if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
            if (-not $process.HasExited) {
                $process.Kill()
            }
            $process.WaitForExit()
            throw "$FailureMessage (timed out after $TimeoutSeconds seconds)"
        }
        if ($process.ExitCode -ne 0) {
            throw "$FailureMessage (exit=$($process.ExitCode))"
        }
    } finally {
        $process.Dispose()
    }
}

function Reset-OwnedDirectory {
    param([string]$Path, [string]$AllowedParent)
    $fullPath = [System.IO.Path]::GetFullPath($Path).TrimEnd('\')
    $fullParent = [System.IO.Path]::GetFullPath($AllowedParent).TrimEnd('\')
    if (-not $fullPath.StartsWith($fullParent + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to clean path outside owned build root: $fullPath"
    }
    if (Test-Path -LiteralPath $fullPath) {
        Remove-Item -LiteralPath $fullPath -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $fullPath | Out-Null
}

function Require-File {
    param([string]$Path, [string]$Description)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Missing $Description`: $Path"
    }
}

function Relative-Path {
    param([string]$BasePath, [string]$ChildPath)
    $baseUri = New-Object System.Uri(([System.IO.Path]::GetFullPath($BasePath).TrimEnd('\') + '\'))
    $childUri = New-Object System.Uri([System.IO.Path]::GetFullPath($ChildPath))
    return [System.Uri]::UnescapeDataString($baseUri.MakeRelativeUri($childUri).ToString()).Replace('/', '\')
}

function Get-FreeTcpPort {
    $listener = New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback, 0)
    $listener.Start()
    try {
        return ([Net.IPEndPoint]$listener.LocalEndpoint).Port
    } finally {
        $listener.Stop()
    }
}

function Test-TcpPort {
    param([int]$Port)
    $client = New-Object Net.Sockets.TcpClient
    try {
        $pending = $client.BeginConnect("127.0.0.1", $Port, $null, $null)
        if (-not $pending.AsyncWaitHandle.WaitOne(200)) {
            return $false
        }
        try {
            $client.EndConnect($pending)
            return $true
        } catch {
            return $false
        }
    } finally {
        $client.Close()
    }
}

function Invoke-PackagedSmokeTest {
    param(
        [string]$FilePath,
        [string]$WorkingDirectory,
        [int]$Port,
        [string]$HealthPath,
        [string]$ExpectedStatus
    )
    $info = New-Object Diagnostics.ProcessStartInfo
    $info.FileName = $FilePath
    $info.WorkingDirectory = $WorkingDirectory
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $process = New-Object Diagnostics.Process
    $process.StartInfo = $info
    if (-not $process.Start()) {
        throw "Unable to start packaged smoke-test process"
    }
    try {
        $ready = $false
        for ($attempt = 0; $attempt -lt 120; $attempt++) {
            Start-Sleep -Milliseconds 250
            if ($process.HasExited) {
                break
            }
            if (Test-TcpPort -Port $Port) {
                $ready = $true
                break
            }
        }
        if (-not $ready) {
            $exitDescription = if ($process.HasExited) { $process.ExitCode } else { "running" }
            throw "Packaged service did not listen on 127.0.0.1:$Port (process=$exitDescription)"
        }
        $response = Invoke-RestMethod -Uri "http://127.0.0.1:$Port$HealthPath" -TimeoutSec 5
        if ([string]$response.status -ne $ExpectedStatus) {
            throw "Packaged health endpoint returned an unexpected status"
        }
    } finally {
        try {
            if (-not $process.HasExited) {
                $process.Kill()
            }
            $process.WaitForExit()
        } finally {
            $process.Dispose()
        }
    }
}

function Compress-ArchiveWithRetry {
    param(
        [string]$SourcePath,
        [string]$DestinationPath,
        [int]$MaxAttempts = 20
    )
    for ($attempt = 1; $attempt -le $MaxAttempts; $attempt++) {
        try {
            Compress-Archive -LiteralPath $SourcePath -DestinationPath $DestinationPath -CompressionLevel Optimal -Force
            return
        } catch {
            if ($attempt -eq $MaxAttempts) {
                throw
            }
            Write-Warning "Archive creation attempt $attempt failed; retrying after Windows releases package files."
            Remove-Item -LiteralPath $DestinationPath -Force -ErrorAction SilentlyContinue
            Start-Sleep -Milliseconds 500
        }
    }
}

function Resolve-InnoCompiler {
    $configured = $env:SERVER_ISCC_EXE
    if ($configured) {
        $configured = [System.IO.Path]::GetFullPath($configured)
        if (-not (Test-Path -LiteralPath $configured -PathType Leaf)) {
            throw "SERVER_ISCC_EXE does not exist: $configured"
        }
        return $configured
    }

    $candidates = @(
        (Join-Path $PSScriptRoot "..\.tools\Inno Setup 6\ISCC.exe")
    )
    if (${env:ProgramFiles(x86)}) {
        $candidates += Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"
    }
    if ($env:ProgramFiles) {
        $candidates += Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe"
    }
    $command = Get-Command "ISCC.exe" -ErrorAction SilentlyContinue
    if ($command) {
        $candidates += $command.Source
    }
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return [System.IO.Path]::GetFullPath($candidate)
        }
    }
    return ""
}

function ConvertTo-InnoString {
    param([string]$Value)
    return $Value.Replace('"', '""')
}

$root = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$buildParent = Join-Path $root "build"
$distParent = Join-Path $root "dist"
$caddyVersionFile = Join-Path $root "deploy\caddy\CADDY_VERSION.txt"
$signingScript = Join-Path $PSScriptRoot "windows_signing.ps1"
Require-File -Path $signingScript -Description "Windows signing helper"
. $signingScript

if ($Target -eq "sm") {
    $displayName = "Server Manager"
    $exeName = "ServerManager"
    $packageName = "ServerManagerPackage"
    $versionPrefix = "v_sm_"
    $sourceDir = Join-Path $root "Server_manager"
    $entryFile = Join-Path $sourceDir "main.py"
    $requirementsFile = Join-Path $sourceDir "requirements-build.txt"
    $caddySource = Join-Path $sourceDir "caddy\caddy.exe"
    $startTemplate = Join-Path $root "deploy\windows\start_sm.bat"
    $localExample = Join-Path $root "deploy\windows\sm.local.bat.example"
    $envExample = Join-Path $root "deploy\windows\sm.env.example"
    $installedLauncher = Join-Path $root "deploy\windows\start_sm_installed.bat"
    $installedLocalConfig = Join-Path $root "deploy\windows\sm.installed.local.bat"
    $innoTemplate = Join-Path $PSScriptRoot "inno\server_manager.iss"
    $installerName = "SC_SM_Setup"
    $setupIconFile = ""
    $buildInfoName = "server_manager_build_info.json"
    $pythonOverride = $env:SM_BUILD_PY
} else {
    $displayName = "Trader Server"
    $exeName = "TraderServer"
    $packageName = "TraderServerPackage"
    $versionPrefix = "v_ts_"
    $sourceDir = Join-Path $root "Trader_Server"
    $entryFile = Join-Path $sourceDir "main.py"
    $requirementsFile = Join-Path $sourceDir "requirements-build.txt"
    $caddySource = Join-Path $sourceDir "caddy\caddy.exe"
    $startTemplate = Join-Path $root "deploy\windows\start_ts.bat"
    $localExample = Join-Path $root "deploy\windows\ts.local.bat.example"
    $envExample = Join-Path $root "deploy\windows\ts.env.example"
    $installedLauncher = Join-Path $root "deploy\windows\start_ts_installed.bat"
    $installedLocalConfig = Join-Path $root "deploy\windows\ts.installed.local.bat"
    $innoTemplate = Join-Path $PSScriptRoot "inno\trader_server.iss"
    $installerName = "SC_TS_Setup"
    $setupIconFile = Join-Path $sourceDir "assets\icons\trader-server.ico"
    $buildInfoName = "trader_server_build_info.json"
    $pythonOverride = $env:TS_BUILD_PY
}

Write-Host "[$displayName Package] Root: $root"
$requiredFiles = @(
    @{ Path = $entryFile; Description = "$displayName entry" },
    @{ Path = $requirementsFile; Description = "$displayName pinned build requirements" },
    @{ Path = $caddySource; Description = "$displayName Caddy executable" },
    @{ Path = $caddyVersionFile; Description = "Caddy version manifest" },
    @{ Path = $startTemplate; Description = "$displayName start script" },
    @{ Path = $localExample; Description = "$displayName local configuration example" },
    @{ Path = $envExample; Description = "$displayName environment example" },
    @{ Path = $installedLauncher; Description = "$displayName installed launcher" },
    @{ Path = $installedLocalConfig; Description = "$displayName installed local configuration" },
    @{ Path = $innoTemplate; Description = "$displayName Inno Setup template" }
)
if ($setupIconFile) {
    $requiredFiles += @{ Path = $setupIconFile; Description = "$displayName setup icon" }
}
foreach ($required in $requiredFiles) {
    Require-File -Path $required.Path -Description $required.Description
}

$installedConfigText = Get-Content -LiteralPath $installedLocalConfig -Raw
$embeddedSecretPattern = '(?im)^\s*set\s+"[^"=]*(?:SECRET_ID|SECRET_KEY|PASSWORD|TASTY_TOKEN|TASTY_SECRET)[^"=]*=([^"\r\n]+)"\s*$'
$embeddedSecrets = [regex]::Matches($installedConfigText, $embeddedSecretPattern)
if ($embeddedSecrets.Count -gt 0) {
    throw "$displayName installed configuration contains a non-empty secret"
}

$expectedCaddyHashLine = Select-String -LiteralPath $caddyVersionFile -Pattern '^SHA256:\s*([A-Fa-f0-9]{64})$'
if (-not $expectedCaddyHashLine) {
    throw "Caddy version manifest does not contain a valid SHA256 entry"
}
$expectedCaddyHash = $expectedCaddyHashLine.Matches[0].Groups[1].Value.ToUpperInvariant()
$actualCaddyHash = (Get-FileHash -LiteralPath $caddySource -Algorithm SHA256).Hash.ToUpperInvariant()
if ($actualCaddyHash -ne $expectedCaddyHash) {
    throw "Caddy SHA256 mismatch"
}

$releaseBuild = Test-BuildFlag "SERVER_RELEASE_BUILD"
if ($releaseBuild) {
    foreach ($forbiddenFlag in @("ALLOW_DIRTY_BUILD", "ALLOW_ARCHIVE_ONLY", "SKIP_PIP", "SKIP_TESTS", "SKIP_SMOKE_TESTS")) {
        if (Test-BuildFlag $forbiddenFlag) {
            throw "$forbiddenFlag is not allowed when SERVER_RELEASE_BUILD=1"
        }
    }
    if (-not (Test-BuildFlag "INNO_COMMERCIAL_LICENSE_CONFIRMED")) {
        throw "INNO_COMMERCIAL_LICENSE_CONFIRMED=1 is required for a release build"
    }
}
$signing = Get-CodeSigningConfiguration
$innoCompiler = Resolve-InnoCompiler
$allowArchiveOnly = Test-BuildFlag "ALLOW_ARCHIVE_ONLY"
if (-not $innoCompiler -and -not $allowArchiveOnly) {
    throw "Inno Setup 6 was not found. Install it, set SERVER_ISCC_EXE, or explicitly set ALLOW_ARCHIVE_ONLY=1 for validation."
}
if (-not $innoCompiler) {
    Write-Warning "Inno Setup 6 was not found; this explicit validation build will produce an archive only."
}
$innoCompilerHash = ""
if ($innoCompiler) {
    $innoSignature = Get-AuthenticodeSignature -FilePath $innoCompiler
    $innoSubject = if ($innoSignature.SignerCertificate) { $innoSignature.SignerCertificate.Subject } else { "" }
    if ([string]$innoSignature.Status -ne "Valid" -or $innoSubject -notlike "*Pyrsys B.V.*") {
        throw "ISCC.exe does not have the expected valid Inno Setup publisher signature"
    }
    $innoCompilerHash = (Get-FileHash -LiteralPath $innoCompiler -Algorithm SHA256).Hash
}

$gitSafeRoot = $root.Replace('\', '/')
$gitStatus = @(& git -c "safe.directory=$gitSafeRoot" status --porcelain 2>$null)
if ($LASTEXITCODE -ne 0) {
    throw "Unable to read Git worktree status"
}
$isDirty = $gitStatus.Count -gt 0
if ($isDirty -and $env:ALLOW_DIRTY_BUILD -ne "1") {
    throw "The worktree is dirty. Commit changes or set ALLOW_DIRTY_BUILD=1 for a non-production validation build."
}
$gitCommit = (& git -c "safe.directory=$gitSafeRoot" rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0) {
    throw "Unable to read Git commit"
}

$buildTimestamp = $env:SERVER_BUILD_TIMESTAMP
if (-not $buildTimestamp) {
    $beijing = [System.TimeZoneInfo]::FindSystemTimeZoneById("China Standard Time")
    $buildTimestamp = [System.TimeZoneInfo]::ConvertTimeFromUtc([DateTime]::UtcNow, $beijing).ToString("yyyyMMddHHmmss")
}
if ($buildTimestamp -notmatch '^\d{14}$') {
    throw "SERVER_BUILD_TIMESTAMP must contain exactly 14 digits"
}
$version = "$versionPrefix$buildTimestamp"
$artifactSuffix = if ($releaseBuild) { "" } else { "_VALIDATION" }

$buildRoot = Join-Path $buildParent $packageName
$distRoot = Join-Path $distParent $packageName
Reset-OwnedDirectory -Path $buildRoot -AllowedParent $buildParent
Reset-OwnedDirectory -Path $distRoot -AllowedParent $distParent
$workDir = Join-Path $buildRoot "work"
$specDir = Join-Path $buildRoot "spec"
$generatedDir = Join-Path $buildRoot "generated"
$appDist = Join-Path $distRoot "app"
$archiveDir = Join-Path $distRoot "archive"
$installerDir = Join-Path $distRoot "installer"
foreach ($directory in @($workDir, $specDir, $generatedDir, $appDist, $archiveDir, $installerDir)) {
    New-Item -ItemType Directory -Force -Path $directory | Out-Null
}

$buildInfoPath = Join-Path $generatedDir $buildInfoName
$buildInfo = [ordered]@{
    product = $Target
    version = $version
    platform = "windows-x64"
    build_timestamp = $buildTimestamp
    artifact_suffix = $artifactSuffix
    git_commit = $gitCommit
    git_dirty = $isDirty
    release_build = $releaseBuild
    inno_commercial_license_confirmed = (Test-BuildFlag "INNO_COMMERCIAL_LICENSE_CONFIRMED")
    installer_enabled = [bool]$innoCompiler
    code_signing_enabled = [bool]$signing.Enabled
    code_signing_required = [bool]$signing.Required
    signing_certificate_thumbprint = $signing.Thumbprint
    signing_certificate_subject = $signing.Subject
    signing_tool_sha256 = $signing.ToolSha256
    timestamp_url = $signing.TimestampUrl
    inno_compiler_sha256 = $innoCompilerHash
    caddy_version = "v2.11.4"
    caddy_sha256 = $actualCaddyHash
}
Write-Utf8NoBom -Path $buildInfoPath -Content ($buildInfo | ConvertTo-Json -Depth 4)

$python = $env:SERVER_BUILD_PY
if (-not $python) {
    $python = $pythonOverride
}
if (-not $python) {
    $venvRoot = Join-Path $env:LOCALAPPDATA "SCServerBuild\$Target-venv"
    $python = Join-Path $venvRoot "Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $python)) {
        Write-Host "[$displayName Package] Creating isolated Python 3.11 build environment..."
        Invoke-Native -FilePath "py" -Arguments @("-3.11", "-m", "venv", $venvRoot) -FailureMessage "Unable to create build environment"
    }
}
Require-File -Path $python -Description "$displayName build Python"
Write-Host "[$displayName Package] Python: $python"
Invoke-Native -FilePath $python -Arguments @(
    "-c",
    "import struct,sys; assert sys.version_info[:2] == (3,11), sys.version; assert struct.calcsize('P')*8 == 64, '64-bit Python required'; print(sys.version)"
) -FailureMessage "Python 3.11 x64 validation failed"

if ($env:SKIP_PIP -ne "1") {
    Write-Host "[$displayName Package] Installing pinned build dependencies..."
    Invoke-Native -FilePath $python -Arguments @("-m", "pip", "install", "--disable-pip-version-check", "-r", $requirementsFile) -FailureMessage "Dependency installation failed"
} else {
    Write-Host "[$displayName Package] SKIP_PIP=1, dependency installation skipped."
}
Invoke-Native -FilePath $python -Arguments @("-m", "pip", "check") -FailureMessage "Installed dependency check failed"

if ($Target -eq "sm") {
    $dependencyProbe = "import PyInstaller,fastapi,uvicorn,starlette,multipart,jinja2,pydantic,certifi,tencentcloud,tastytrade,tzdata; print(PyInstaller.__version__, fastapi.__version__, uvicorn.__version__)"
} else {
    $dependencyProbe = "import PyInstaller,fastapi,uvicorn,starlette,websockets,pydantic,httpx,certifi,tastytrade,ibapi,PySide6,tzdata; print(PyInstaller.__version__, fastapi.__version__, uvicorn.__version__, PySide6.__version__)"
}
Invoke-Native -FilePath $python -Arguments @("-c", $dependencyProbe) -FailureMessage "Runtime dependency import validation failed"

Push-Location $root
try {
    if ($env:SKIP_TESTS -ne "1") {
        Invoke-Native -FilePath $python -Arguments @("-m", "unittest", "tests.test_server_packaging") -FailureMessage "Server packaging tests failed"
        if ($env:RUN_FULL_TESTS -eq "1") {
            Invoke-Native -FilePath $python -Arguments @("-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py") -FailureMessage "Full regression tests failed"
        }
    }

    $pyInstallerArgs = @(
        "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--name", $exeName,
        "--distpath", $appDist,
        "--workpath", $workDir,
        "--specpath", $specDir,
        "--paths", $root,
        "--paths", $sourceDir,
        "--collect-all", "uvicorn",
        "--collect-all", "tastytrade",
        "--collect-all", "tzdata"
    )

    if ($Target -eq "sm") {
        $pyInstallerArgs += @(
            "--console",
            "--add-data", ((Join-Path $sourceDir "templates") + ";templates"),
            "--add-data", ((Join-Path $sourceDir "resources") + ";resources"),
            "--add-data", ($buildInfoPath + ";."),
            "--hidden-import", "tencentcloud.common.credential",
            "--hidden-import", "tencentcloud.common.profile.client_profile",
            "--hidden-import", "tencentcloud.common.profile.http_profile",
            "--hidden-import", "tencentcloud.dnspod.v20210323.dnspod_client",
            "--hidden-import", "tencentcloud.dnspod.v20210323.models"
        )
    } else {
        $pyInstallerArgs += @(
            "--windowed",
            "--disable-windowed-traceback",
            "--icon", $setupIconFile,
            "--add-data", ((Join-Path $sourceDir "assets") + ";Trader_Server\assets"),
            "--add-data", ($buildInfoPath + ";."),
            "--collect-all", "websockets",
            "--collect-submodules", "Trader_Server",
            "--collect-submodules", "ibapi"
        )
    }
    $pyInstallerArgs += $entryFile
    Write-Host "[$displayName Package] Building PyInstaller onedir application..."
    Invoke-Native -FilePath $python -Arguments $pyInstallerArgs -FailureMessage "PyInstaller build failed"
} finally {
    Pop-Location
}

$appOut = Join-Path $appDist $exeName
$appExe = Join-Path $appOut "$exeName.exe"
Require-File -Path $appExe -Description "$displayName packaged executable"
$caddyOut = Join-Path $appOut "caddy"
New-Item -ItemType Directory -Force -Path $caddyOut | Out-Null
Copy-Item -LiteralPath $caddySource -Destination (Join-Path $caddyOut "caddy.exe")
Copy-Item -LiteralPath $caddyVersionFile -Destination (Join-Path $caddyOut "CADDY_VERSION.txt")
Copy-Item -LiteralPath $startTemplate -Destination (Join-Path $appOut (Split-Path $startTemplate -Leaf))
Copy-Item -LiteralPath $localExample -Destination (Join-Path $appOut (Split-Path $localExample -Leaf))
Copy-Item -LiteralPath $envExample -Destination (Join-Path $appOut (Split-Path $envExample -Leaf))
Copy-Item -LiteralPath $buildInfoPath -Destination (Join-Path $appOut "BUILD_INFO.json")

$packagedCaddyHash = (Get-FileHash -LiteralPath (Join-Path $caddyOut "caddy.exe") -Algorithm SHA256).Hash.ToUpperInvariant()
if ($packagedCaddyHash -ne $expectedCaddyHash) {
    throw "Packaged Caddy SHA256 mismatch"
}

if ($signing.Enabled) {
    Write-Host "[$displayName Package] Signing packaged executable..."
    Invoke-AuthenticodeSigning -Path $appExe -Configuration $signing
} else {
    Write-Warning "Packaged executable is unsigned. This output is not eligible for signed production release."
}

$selfTestData = Join-Path $buildRoot "selftest-data"
New-Item -ItemType Directory -Force -Path $selfTestData | Out-Null
if ($Target -eq "sm") {
    $env:SM_ENVIRONMENT = "selftest"
    $env:SM_DATA_DIR = $selfTestData
    $env:SERVER_MANAGER_DB_PATH = Join-Path $selfTestData "server_manager.db"
    $env:SM_SOFTWARE_STORAGE_DIR = Join-Path $selfTestData "software"
    $env:SM_CADDY_AUTO_MANAGE = "0"
    $env:SM_CADDY_REQUIRED = "0"
    $env:SM_DNSPOD_MODE = "disabled"
} else {
    $env:TS_ENVIRONMENT = "selftest"
    $env:TS_DATA_DIR = $selfTestData
    $env:TS_CADDY_AUTO_MANAGE = "0"
    $env:TS_CADDY_REQUIRED = "0"
    $env:QT_QPA_PLATFORM = "offscreen"
}
Write-Host "[$displayName Package] Running frozen package self-test..."
if ($Target -eq "ts") {
    Invoke-GuiAndWait -FilePath $appExe -Arguments @("--package-self-test") -WorkingDirectory $appOut -FailureMessage "Frozen package self-test failed"
} else {
    Invoke-Native -FilePath $appExe -Arguments @("--package-self-test") -FailureMessage "Frozen package self-test failed"
}

if ($env:SKIP_SMOKE_TESTS -ne "1") {
    $smokeData = Join-Path $buildRoot "smoke-data"
    $smokePort = Get-FreeTcpPort
    New-Item -ItemType Directory -Force -Path $smokeData | Out-Null
    if ($Target -eq "sm") {
        $env:SM_DATA_DIR = $smokeData
        $env:SERVER_MANAGER_DB_PATH = Join-Path $smokeData "server_manager.db"
        $env:SM_SOFTWARE_STORAGE_DIR = Join-Path $smokeData "software"
        $env:SM_BOOTSTRAP_ADMIN_PASSWORD = "PackageSmokeOnly-NotForProduction"
        $env:SERVER_HOST = "127.0.0.1"
        $env:SERVER_PORT = [string]$smokePort
        $healthPath = "/ping"
        $expectedStatus = "pong"
    } else {
        $env:TS_DATA_DIR = $smokeData
        $env:TS_MANAGER_URL = "https://127.0.0.1:9"
        $env:TS_BIND_HOST = "127.0.0.1"
        $env:TS_WS_PORT = [string]$smokePort
        $env:TS_FINANCE_ENABLED = "0"
        $healthPath = "/health"
        $expectedStatus = "ok"
    }
    Write-Host "[$displayName Package] Running packaged service smoke test..."
    Invoke-PackagedSmokeTest -FilePath $appExe -WorkingDirectory $appOut -Port $smokePort -HealthPath $healthPath -ExpectedStatus $expectedStatus
}

$blocked = New-Object System.Collections.Generic.List[string]
$files = @(Get-ChildItem -LiteralPath $appOut -Recurse -Force -File)
foreach ($file in $files) {
    $relative = (Relative-Path -BasePath $appOut -ChildPath $file.FullName).Replace('\', '/').ToLowerInvariant()
    if (
        $relative -match '(^|/)(server_manager\.db(?:-wal|-shm)?|\.register_state\.json)$' -or
        $relative -match '(^|/)(server_manager|trader_server)/data/config\.json$' -or
        $relative -match '^data/config\.json$' -or
        $relative -match '(^|/)(sm|ts)\.local\.bat$' -or
        $relative -match '(^|/)\.env(?:\.|$)' -or
        $relative -match '(^|/)(server_manager|trader_server)/data/' -or
        $relative -match '^data/' -or
        $relative -match '^caddy/(data|config|logs)/' -or
        $relative -match '\.(db|sqlite|log|jsonl)$' -or
        $relative -match '(^|/)caddyfile$'
    ) {
        $blocked.Add($relative)
    }
}
if ($blocked.Count -gt 0) {
    throw "Forbidden runtime or secret-bearing files found in package: $($blocked -join ', ')"
}

$allowedRootNames = @(
    "$exeName.exe",
    "_internal",
    "caddy",
    (Split-Path $startTemplate -Leaf),
    (Split-Path $localExample -Leaf),
    (Split-Path $envExample -Leaf),
    "BUILD_INFO.json"
)
$unexpectedRoot = @(Get-ChildItem -LiteralPath $appOut -Force | Where-Object { $allowedRootNames -notcontains $_.Name })
if ($unexpectedRoot.Count -gt 0) {
    throw "Unexpected package root entries: $($unexpectedRoot.Name -join ', ')"
}

$archiveName = "SC_{0}_Windows_x64_{1}{2}.zip" -f $Target.ToUpperInvariant(), $version, $artifactSuffix
$archivePath = Join-Path $archiveDir $archiveName
Compress-ArchiveWithRetry -SourcePath $appOut -DestinationPath $archivePath
Require-File -Path $archivePath -Description "$displayName deployment archive"

$installerPath = ""
if ($innoCompiler) {
    $innoWrapperPath = Join-Path $generatedDir "$Target-installer.generated.iss"
    $innoLines = @(
        "#define AppVersion `"$(ConvertTo-InnoString $version)`"",
        "#define SourceDir `"$(ConvertTo-InnoString $appOut)`"",
        "#define OutputDir `"$(ConvertTo-InnoString $installerDir)`"",
        "#define InstalledLauncher `"$(ConvertTo-InnoString $installedLauncher)`"",
        "#define InstalledLocalConfig `"$(ConvertTo-InnoString $installedLocalConfig)`""
    )
    $innoLines += "#define ArtifactSuffix `"$(ConvertTo-InnoString $artifactSuffix)`""
    if ($setupIconFile) {
        $innoLines += "#define SetupIconFile `"$(ConvertTo-InnoString $setupIconFile)`""
    }
    if ($signing.Enabled) {
        $innoLines += "#define EnableSigning"
    }
    $innoLines += "#include `"$(ConvertTo-InnoString $innoTemplate)`""
    Write-Utf8NoBom -Path $innoWrapperPath -Content (($innoLines -join "`r`n") + "`r`n")

    Write-Host "[$displayName Package] Building Inno Setup installer..."
    $innoArguments = @("/Qp")
    if ($signing.Enabled) {
        $innoSignCommand = Get-InnoSignToolCommand -Configuration $signing
        $innoArguments += "/Sscsign=$innoSignCommand"
    }
    $innoArguments += $innoWrapperPath
    Invoke-Native -FilePath $innoCompiler -Arguments $innoArguments -FailureMessage "Inno Setup build failed"
    $installerPath = Join-Path $installerDir "${installerName}_${version}${artifactSuffix}.exe"
    Require-File -Path $installerPath -Description "$displayName installer"
    if ($signing.Enabled) {
        Write-Host "[$displayName Package] Verifying signed installer..."
        Assert-AuthenticodeSignature -Path $installerPath -Configuration $signing
    } else {
        Write-Warning "Installer is unsigned. This output is for validation only."
    }
}

$manifestFiles = foreach ($file in $files) {
    [ordered]@{
        path = (Relative-Path -BasePath $appOut -ChildPath $file.FullName).Replace('\', '/')
        size = $file.Length
        sha256 = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash
    }
}
$artifactPaths = @($archivePath)
if ($installerPath) {
    $artifactPaths += $installerPath
}
$artifactEntries = foreach ($artifactPath in $artifactPaths) {
    [ordered]@{
        type = if ($artifactPath -eq $archivePath) { "archive" } else { "installer" }
        path = (Relative-Path -BasePath $distRoot -ChildPath $artifactPath).Replace('\', '/')
        size = (Get-Item -LiteralPath $artifactPath).Length
        sha256 = (Get-FileHash -LiteralPath $artifactPath -Algorithm SHA256).Hash
        authenticode_signed = [bool]($signing.Enabled -and $artifactPath -ne $archivePath)
    }
}
$manifest = [ordered]@{
    build = $buildInfo
    files = @($manifestFiles)
    artifacts = @($artifactEntries)
}
$manifestPath = Join-Path $distRoot "MANIFEST.json"
Write-Utf8NoBom -Path $manifestPath -Content ($manifest | ConvertTo-Json -Depth 6)

$checksumPath = Join-Path $distRoot "SHA256SUMS.txt"
$checksumLines = foreach ($artifact in $artifactEntries) {
    "$($artifact.sha256)  $($artifact.path)"
}
[System.IO.File]::WriteAllText($checksumPath, (($checksumLines -join "`r`n") + "`r`n"), [System.Text.Encoding]::ASCII)

Write-Host "[$displayName Package] Version: $version"
Write-Host "[$displayName Package] Application: $appExe"
Write-Host "[$displayName Package] Archive: $archivePath"
if ($installerPath) {
    Write-Host "[$displayName Package] Installer: $installerPath"
}
Write-Host "[$displayName Package] Manifest: $manifestPath"
Write-Host "[$displayName Package] Checksums: $checksumPath"
Write-Host "[$displayName Package] Done."
