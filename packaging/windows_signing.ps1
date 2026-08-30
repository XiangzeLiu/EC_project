function Test-BuildFlag {
    param([string]$Name)
    $value = [Environment]::GetEnvironmentVariable($Name)
    return $value -and $value.Trim().ToLowerInvariant() -in @("1", "true", "yes", "on")
}

function Resolve-CodeSigningTool {
    $configured = $env:SERVER_SIGNTOOL_EXE
    if ($configured) {
        $configured = [System.IO.Path]::GetFullPath($configured)
        if (-not (Test-Path -LiteralPath $configured -PathType Leaf)) {
            throw "SERVER_SIGNTOOL_EXE does not exist: $configured"
        }
        return $configured
    }

    $command = Get-Command "signtool.exe" -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }

    $localSdkRoot = Join-Path $PSScriptRoot "..\.tools\WindowsSDKBuildTools"
    if (Test-Path -LiteralPath $localSdkRoot -PathType Container) {
        $localTools = @(
            Get-ChildItem -LiteralPath $localSdkRoot -Recurse -Filter "signtool.exe" |
                Where-Object { $_.FullName -match '\\x64\\signtool\.exe$' } |
                Sort-Object FullName -Descending
        )
        if ($localTools.Count -gt 0) {
            return $localTools[0].FullName
        }
    }

    $kitRoots = @()
    if (${env:ProgramFiles(x86)}) {
        $kitRoots += Join-Path ${env:ProgramFiles(x86)} "Windows Kits\10\bin"
    }
    if ($env:ProgramFiles) {
        $kitRoots += Join-Path $env:ProgramFiles "Windows Kits\10\bin"
    }
    foreach ($kitRoot in $kitRoots) {
        if (-not (Test-Path -LiteralPath $kitRoot -PathType Container)) {
            continue
        }
        $versionDirectories = @(Get-ChildItem -LiteralPath $kitRoot -Directory | Sort-Object Name -Descending)
        foreach ($directory in $versionDirectories) {
            $candidate = Join-Path $directory.FullName "x64\signtool.exe"
            if (Test-Path -LiteralPath $candidate -PathType Leaf) {
                return $candidate
            }
        }
        $unversioned = Join-Path $kitRoot "x64\signtool.exe"
        if (Test-Path -LiteralPath $unversioned -PathType Leaf) {
            return $unversioned
        }
    }
    throw "Windows SDK signtool.exe was not found. Set SERVER_SIGNTOOL_EXE after installing the Windows SDK signing tools."
}

function Resolve-CodeSigningCertificate {
    param([string]$Thumbprint)

    $normalized = ($Thumbprint -replace '\s', '').ToUpperInvariant()
    if ($normalized -notmatch '^[A-F0-9]{40,64}$') {
        throw "SERVER_SIGN_CERT_THUMBPRINT is not a valid certificate thumbprint"
    }

    foreach ($storeLocation in @("CurrentUser", "LocalMachine")) {
        $certificatePath = "Cert:\$storeLocation\My\$normalized"
        $certificate = Get-Item -LiteralPath $certificatePath -ErrorAction SilentlyContinue
        if (-not $certificate) {
            continue
        }
        if (-not $certificate.HasPrivateKey) {
            throw "The configured signing certificate does not expose a private key"
        }
        $now = Get-Date
        if ($certificate.NotBefore -gt $now -or $certificate.NotAfter -lt $now) {
            throw "The configured signing certificate is not currently valid"
        }
        $codeSigningOid = "1.3.6.1.5.5.7.3.3"
        $ekuOids = @($certificate.EnhancedKeyUsageList | ForEach-Object { $_.ObjectId.Value })
        if ($ekuOids -notcontains $codeSigningOid) {
            throw "The configured certificate is not valid for code signing"
        }
        return [pscustomobject]@{
            Certificate = $certificate
            StoreLocation = $storeLocation
        }
    }
    throw "The configured signing certificate was not found in CurrentUser or LocalMachine personal stores"
}

function Get-CodeSigningConfiguration {
    $required = (Test-BuildFlag "SERVER_RELEASE_BUILD") -or (Test-BuildFlag "REQUIRE_CODE_SIGNING")
    $thumbprint = ($env:SERVER_SIGN_CERT_THUMBPRINT -replace '\s', '').ToUpperInvariant()
    if (-not $thumbprint) {
        if ($required) {
            throw "Code signing is required, but SERVER_SIGN_CERT_THUMBPRINT is not configured"
        }
        return [pscustomobject]@{
            Enabled = $false
            Required = $false
            ToolPath = ""
            ToolSha256 = ""
            Thumbprint = ""
            Subject = ""
            StoreLocation = ""
            TimestampUrl = ""
        }
    }

    $resolved = Resolve-CodeSigningCertificate -Thumbprint $thumbprint
    $timestampUrl = ($env:SERVER_TIMESTAMP_URL | ForEach-Object { $_.Trim() })
    if (-not $timestampUrl) {
        $timestampUrl = "http://timestamp.digicert.com"
    }
    $timestampUri = $null
    $validTimestampUri = [System.Uri]::TryCreate(
        $timestampUrl,
        [System.UriKind]::Absolute,
        [ref]$timestampUri
    )
    if (
        -not $validTimestampUri -or
        $timestampUri.Scheme -notin @("http", "https") -or
        $timestampUrl -match '[\s"]'
    ) {
        throw "SERVER_TIMESTAMP_URL must use an HTTP or HTTPS URL"
    }
    $toolPath = Resolve-CodeSigningTool
    $toolSignature = Get-AuthenticodeSignature -FilePath $toolPath
    $toolSubject = if ($toolSignature.SignerCertificate) { $toolSignature.SignerCertificate.Subject } else { "" }
    if ([string]$toolSignature.Status -ne "Valid" -or $toolSubject -notlike "*Microsoft Corporation*") {
        throw "signtool.exe does not have a valid Microsoft Authenticode signature"
    }
    return [pscustomobject]@{
        Enabled = $true
        Required = $required
        ToolPath = $toolPath
        ToolSha256 = (Get-FileHash -LiteralPath $toolPath -Algorithm SHA256).Hash
        Thumbprint = $resolved.Certificate.Thumbprint.ToUpperInvariant()
        Subject = $resolved.Certificate.Subject
        StoreLocation = $resolved.StoreLocation
        TimestampUrl = $timestampUrl
    }
}

function Get-InnoSignToolCommand {
    param(
        [Parameter(Mandatory = $true)]
        $Configuration
    )
    if (-not $Configuration.Enabled) {
        throw "An enabled signing configuration is required for Inno Setup signing"
    }
    $arguments = @(
        ('"' + $Configuration.ToolPath + '"'),
        "sign"
    )
    if ($Configuration.StoreLocation -eq "LocalMachine") {
        $arguments += "/sm"
    }
    $arguments += @(
        "/sha1", $Configuration.Thumbprint,
        "/fd", "SHA256",
        "/tr", ('"' + $Configuration.TimestampUrl + '"'),
        "/td", "SHA256",
        "/v",
        '$f'
    )
    return $arguments -join " "
}

function Assert-AuthenticodeSignature {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        $Configuration
    )
    & $Configuration.ToolPath @("verify", "/pa", "/all", "/v", $Path)
    if ($LASTEXITCODE -ne 0) {
        throw "Authenticode verification failed for $Path (exit=$LASTEXITCODE)"
    }

    $signature = Get-AuthenticodeSignature -FilePath $Path
    if ([string]$signature.Status -ne "Valid") {
        throw "PowerShell signature verification failed for $Path`: $($signature.StatusMessage)"
    }
    if (-not $signature.SignerCertificate -or $signature.SignerCertificate.Thumbprint.ToUpperInvariant() -ne $Configuration.Thumbprint) {
        throw "The signer certificate does not match SERVER_SIGN_CERT_THUMBPRINT"
    }
    if (-not $signature.TimeStamperCertificate) {
        throw "The Authenticode signature does not contain a trusted timestamp"
    }
}

function Invoke-AuthenticodeSigning {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        $Configuration
    )

    if (-not $Configuration.Enabled) {
        throw "Invoke-AuthenticodeSigning was called without an enabled signing configuration"
    }
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Signing target does not exist: $Path"
    }

    $signArguments = @("sign")
    if ($Configuration.StoreLocation -eq "LocalMachine") {
        $signArguments += "/sm"
    }
    $signArguments += @(
        "/sha1", $Configuration.Thumbprint,
        "/fd", "SHA256",
        "/tr", $Configuration.TimestampUrl,
        "/td", "SHA256",
        "/v",
        $Path
    )
    & $Configuration.ToolPath @signArguments
    if ($LASTEXITCODE -ne 0) {
        throw "Authenticode signing failed for $Path (exit=$LASTEXITCODE)"
    }

    Assert-AuthenticodeSignature -Path $Path -Configuration $Configuration
}
