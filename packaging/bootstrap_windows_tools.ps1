$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Assert-FileSha256 {
    param([string]$Path, [string]$Expected)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Required file does not exist: $Path"
    }
    $actual = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToUpperInvariant()
    if ($actual -ne $Expected.ToUpperInvariant()) {
        throw "SHA256 mismatch for $Path"
    }
}

function Assert-ValidPublisherSignature {
    param([string]$Path, [string]$SubjectContains)
    $signature = Get-AuthenticodeSignature -FilePath $Path
    $subject = if ($signature.SignerCertificate) { $signature.SignerCertificate.Subject } else { "" }
    if ([string]$signature.Status -ne "Valid" -or $subject -notlike "*$SubjectContains*") {
        throw "Unexpected Authenticode publisher for $Path"
    }
}

function Get-VerifiedDownload {
    param([string]$Url, [string]$Path, [string]$Sha256)
    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        try {
            Assert-FileSha256 -Path $Path -Expected $Sha256
            return
        } catch {
            Remove-Item -LiteralPath $Path -Force
        }
    }
    Invoke-WebRequest -Uri $Url -OutFile $Path -MaximumRedirection 5
    Assert-FileSha256 -Path $Path -Expected $Sha256
}

$root = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$toolsRoot = Join-Path $root ".tools"
$downloadRoot = Join-Path $root ".tmp\packaging-tools"
$lockPath = Join-Path $PSScriptRoot "windows_tools.lock.json"
if (-not (Test-Path -LiteralPath $lockPath -PathType Leaf)) {
    throw "Packaging tool lock file is missing"
}
$lock = Get-Content -LiteralPath $lockPath -Raw -Encoding UTF8 | ConvertFrom-Json
New-Item -ItemType Directory -Force -Path $toolsRoot, $downloadRoot | Out-Null

$inno = $lock.inno_setup
$innoDir = Join-Path $toolsRoot "Inno Setup 6"
$iscc = Join-Path $innoDir "ISCC.exe"
if (Test-Path -LiteralPath $iscc -PathType Leaf) {
    Assert-FileSha256 -Path $iscc -Expected $inno.compiler_sha256
    Assert-ValidPublisherSignature -Path $iscc -SubjectContains $inno.signer_contains
} else {
    if (Test-Path -LiteralPath $innoDir) {
        throw "Inno Setup target exists but ISCC.exe is missing: $innoDir"
    }
    $innoInstaller = Join-Path $downloadRoot "innosetup-$($inno.version).exe"
    Get-VerifiedDownload -Url $inno.url -Path $innoInstaller -Sha256 $inno.installer_sha256
    Assert-ValidPublisherSignature -Path $innoInstaller -SubjectContains $inno.signer_contains
    $arguments = @(
        "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-", "/CURRENTUSER", "/NOICONS",
        "/DIR=`"$innoDir`""
    )
    $process = Start-Process -FilePath $innoInstaller -ArgumentList $arguments -Wait -PassThru
    if ($process.ExitCode -ne 0) {
        throw "Inno Setup installation failed (exit=$($process.ExitCode))"
    }
    Assert-FileSha256 -Path $iscc -Expected $inno.compiler_sha256
    Assert-ValidPublisherSignature -Path $iscc -SubjectContains $inno.signer_contains
}

$sdk = $lock.windows_sdk_build_tools
$sdkDir = Join-Path $toolsRoot "WindowsSDKBuildTools\$($sdk.version)"
$signtool = Join-Path $sdkDir ($sdk.signtool_relative_path.Replace('/', '\'))
if (Test-Path -LiteralPath $signtool -PathType Leaf) {
    Assert-FileSha256 -Path $signtool -Expected $sdk.signtool_sha256
    Assert-ValidPublisherSignature -Path $signtool -SubjectContains $sdk.signer_contains
} else {
    if (Test-Path -LiteralPath $sdkDir) {
        throw "Windows SDK target exists but signtool.exe is missing: $sdkDir"
    }
    $sdkPackage = Join-Path $downloadRoot "microsoft.windows.sdk.buildtools.$($sdk.version).nupkg"
    Invoke-WebRequest -Uri $sdk.url -OutFile $sdkPackage
    $sha512 = [System.Security.Cryptography.SHA512]::Create()
    try {
        $stream = [System.IO.File]::OpenRead($sdkPackage)
        try {
            $packageHash = [Convert]::ToBase64String($sha512.ComputeHash($stream))
        } finally {
            $stream.Dispose()
        }
    } finally {
        $sha512.Dispose()
    }
    if ($packageHash -ne $sdk.package_sha512_base64) {
        throw "SHA512 mismatch for $sdkPackage"
    }
    New-Item -ItemType Directory -Force -Path (Split-Path $sdkDir -Parent) | Out-Null
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [System.IO.Compression.ZipFile]::ExtractToDirectory($sdkPackage, $sdkDir)
    Assert-FileSha256 -Path $signtool -Expected $sdk.signtool_sha256
    Assert-ValidPublisherSignature -Path $signtool -SubjectContains $sdk.signer_contains
}

Write-Host "Packaging tools are ready."
Write-Host "Inno Setup: $iscc"
Write-Host "SignTool: $signtool"
