[CmdletBinding()]
param(
    [string]$InstallRoot,
    [string]$Wheelhouse,
    [string]$ManifestPath,
    [string]$RuntimeRoot,
    [string]$Python,
    [string]$Git,
    [string]$GithubClientId,
    [long]$CoordinatorIntegrationId,
    [switch]$UseBundledPython,
    [switch]$Recreate,
    [switch]$ValidateOnly,
    [switch]$NoPathUpdate
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-FullPath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    return [System.IO.Path]::GetFullPath($Path)
}

function Test-PathInside {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Child,
        [Parameter(Mandatory = $true)]
        [string]$Parent
    )

    $childPath = (Get-FullPath $Child).TrimEnd('\', '/')
    $parentPath = (Get-FullPath $Parent).TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
    return $childPath.StartsWith(
        $parentPath,
        [System.StringComparison]::OrdinalIgnoreCase
    )
}

function Invoke-NativeChecked {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments,
        [Parameter(Mandatory = $true)]
        [string]$Step
    )

    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Step failed with exit code $LASTEXITCODE."
    }
}

function Assert-SafeVenvTarget {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$ExpectedLeaf,
        [Parameter(Mandatory = $true)]
        [string]$Framework,
        [Parameter(Mandatory = $true)]
        [string]$Install
    )

    $fullPath = Get-FullPath $Path
    $leaf = Split-Path -Leaf $fullPath
    $root = [System.IO.Path]::GetPathRoot($fullPath)
    if (
        $leaf -ne $ExpectedLeaf -or
        $fullPath -eq (Get-FullPath $Framework) -or
        $fullPath -eq (Get-FullPath $Install) -or
        $fullPath -eq $root
    ) {
        throw "Unsafe venv target: $fullPath"
    }
}

$frameworkRoot = Get-FullPath $PSScriptRoot
$marker = Join-Path $frameworkRoot '.aria-root'
$pyproject = Join-Path $frameworkRoot 'pyproject.toml'
if (-not (Test-Path -LiteralPath $marker -PathType Leaf)) {
    throw "ARIA framework marker is missing: $marker"
}
if (-not (Test-Path -LiteralPath $pyproject -PathType Leaf)) {
    throw "ARIA pyproject.toml is missing: $pyproject"
}

$pyprojectText = Get-Content -LiteralPath $pyproject -Raw
$versionMatch = [regex]::Match(
    $pyprojectText,
    '(?m)^version\s*=\s*"(?<version>[^"]+)"\s*$'
)
if (-not $versionMatch.Success) {
    throw "Cannot read ARIA version from $pyproject"
}
$version = $versionMatch.Groups['version'].Value
if ($version -ne '1.5.5') {
    throw "This installer is for ARIA 1.5.5, framework reports $version."
}

function Test-Python312 {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,
        [string[]]$Prefix = @()
    )

    try {
        & $FilePath @(
            $Prefix +
            @(
                '-I',
                '-c',
                'import sys;raise SystemExit(0 if sys.version_info[:2] == (3,12) else 2)'
            )
        ) | Out-Null
        return $LASTEXITCODE -eq 0
    }
    catch {
        return $false
    }
}

if ([string]::IsNullOrWhiteSpace($InstallRoot)) {
    $InstallRoot = Split-Path -Parent $frameworkRoot
}
$installRootPath = Get-FullPath $InstallRoot

if ([string]::IsNullOrWhiteSpace($RuntimeRoot)) {
    if (-not [string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
        $RuntimeRoot = Join-Path $env:LOCALAPPDATA 'ARIA-Codex'
    }
    elseif (-not [string]::IsNullOrWhiteSpace($env:USERPROFILE)) {
        $RuntimeRoot = Join-Path $env:USERPROFILE 'AppData\Local\ARIA-Codex'
    }
    else {
        throw 'RuntimeRoot is required when LOCALAPPDATA and USERPROFILE are unavailable.'
    }
}
$runtimeRootPath = Get-FullPath $RuntimeRoot
$venvLeaf = "venv-$version"
$venvPath = Join-Path $installRootPath $venvLeaf
$venvPath = Get-FullPath $venvPath
$installReceiptPath = Join-Path $installRootPath 'install-receipt.json'
$priorInstallReceipt = $null
if (Test-Path -LiteralPath $installReceiptPath -PathType Leaf) {
    $priorInstallReceipt = Get-Content -LiteralPath $installReceiptPath -Raw | ConvertFrom-Json
    if (
        $priorInstallReceipt.schema_version -ne 1 -or
        $priorInstallReceipt.version -ne $version -or
        (Get-FullPath ([string]$priorInstallReceipt.install_root)) -ne $installRootPath
    ) {
        throw "Existing ARIA install receipt is invalid: $installReceiptPath"
    }
}
if (
    $installRootPath -eq $frameworkRoot -or
    (Test-PathInside -Child $installRootPath -Parent $frameworkRoot)
) {
    throw "InstallRoot must be outside the immutable framework root: $frameworkRoot"
}
if (
    $runtimeRootPath -eq $frameworkRoot -or
    (Test-PathInside -Child $runtimeRootPath -Parent $frameworkRoot)
) {
    throw "RuntimeRoot must be outside the immutable framework root: $frameworkRoot"
}

$releaseRoot = Join-Path $frameworkRoot "releases\$version"
if ([string]::IsNullOrWhiteSpace($ManifestPath)) {
    $manifestPath = Join-Path $releaseRoot 'manifest.json'
}
else {
    $manifestPath = Get-FullPath $ManifestPath
}
if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
    throw "Release manifest is missing: $manifestPath"
}
$manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
if ($manifest.schema_version -notin @(2, 3)) {
    throw "Unsupported installer manifest schema: $($manifest.schema_version)"
}
if ($manifest.version -ne $version) {
    throw "Release manifest version $($manifest.version) does not match framework $version."
}
if ($manifest.verification.status -ne 'accepted') {
    throw "Release manifest is not accepted: $($manifest.verification.status)"
}

if ([string]::IsNullOrWhiteSpace($Wheelhouse)) {
    $Wheelhouse = $releaseRoot
}
$wheelhousePath = Get-FullPath $Wheelhouse
if (-not (Test-Path -LiteralPath $wheelhousePath -PathType Container)) {
    throw "Wheelhouse does not exist: $wheelhousePath"
}

$manifestFiles = @($manifest.files)
if ($manifestFiles.Count -lt 1) {
    throw "Release manifest does not declare wheelhouse files."
}
$declaredFileNames = @($manifestFiles | ForEach-Object { [string]$_.name })
foreach ($entry in $manifestFiles) {
    $artifact = Join-Path $wheelhousePath $entry.name
    if (-not (Test-Path -LiteralPath $artifact -PathType Leaf)) {
        throw "Required release artifact is missing: $artifact"
    }
    $actualHash = (
        Get-FileHash -LiteralPath $artifact -Algorithm SHA256
    ).Hash.ToLowerInvariant()
    $expectedHash = ([string]$entry.sha256).ToLowerInvariant()
    if ($expectedHash -notmatch '^[0-9a-f]{64}$') {
        throw "Invalid SHA-256 in release manifest for $($entry.name)."
    }
    if ($actualHash -ne $expectedHash) {
        throw "SHA-256 mismatch for $($entry.name): expected $expectedHash, got $actualHash."
    }
}

$packageWheel = Join-Path $wheelhousePath $manifest.wheel
if ($declaredFileNames -notcontains [string]$manifest.wheel) {
    throw "ARIA wheel is not hash-bound by the release manifest: $($manifest.wheel)"
}
if (-not (Test-Path -LiteralPath $packageWheel -PathType Leaf)) {
    throw "ARIA wheel is missing: $packageWheel"
}

$manifestPropertyNames = @($manifest.PSObject.Properties.Name)
if ([string]::IsNullOrWhiteSpace($GithubClientId) -and $manifestPropertyNames -contains 'provider') {
    $GithubClientId = [string]$manifest.provider.github_client_id
}
if ($CoordinatorIntegrationId -le 0 -and $manifestPropertyNames -contains 'provider') {
    $CoordinatorIntegrationId = [long]$manifest.provider.coordinator_integration_id
}
if (
    (-not [string]::IsNullOrWhiteSpace($GithubClientId) -and $CoordinatorIntegrationId -le 0) -or
    ([string]::IsNullOrWhiteSpace($GithubClientId) -and $CoordinatorIntegrationId -gt 0)
) {
    throw 'GitHub client id and coordinator integration id must be supplied together.'
}

$gitExecutable = $null
if (-not [string]::IsNullOrWhiteSpace($Git)) {
    $gitExecutable = Get-FullPath $Git
    if (-not (Test-Path -LiteralPath $gitExecutable -PathType Leaf)) {
        throw "Specified Git executable does not exist: $gitExecutable"
    }
}
else {
    $gitCommand = Get-Command 'git.exe' -ErrorAction SilentlyContinue
    if ($null -ne $gitCommand) {
        $gitExecutable = $gitCommand.Source
    }
}
if ($null -eq $gitExecutable) {
    throw 'Git for Windows was not found. Install Git or pass -Git C:\path\to\git.exe.'
}
$gitVersion = & $gitExecutable '--version'
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace([string]$gitVersion)) {
    throw "Cannot execute Git: $gitExecutable"
}

$pythonExecutable = $null
$pythonPrefix = @()
$bundledPythonOwned = $false
$bundledPythonInstalledNow = $false
$bundledPythonTarget = $null
$bundledPythonKind = $null
if ($UseBundledPython -and -not [string]::IsNullOrWhiteSpace($Python)) {
    throw '-UseBundledPython cannot be combined with -Python.'
}
if (-not [string]::IsNullOrWhiteSpace($Python)) {
    $pythonExecutable = Get-FullPath $Python
    if (-not (Test-Path -LiteralPath $pythonExecutable -PathType Leaf)) {
        throw "Specified Python executable does not exist: $pythonExecutable"
    }
}
else {
    $pythonCandidates = @()
    if (-not $UseBundledPython) {
        $pythonLauncher = Get-Command 'py.exe' -ErrorAction SilentlyContinue
        if ($null -ne $pythonLauncher) {
            $pythonCandidates += @{
                executable = [string]$pythonLauncher.Source
                prefix = @('-3.12')
            }
        }
        if (-not [string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
            $pythonCandidates += @{
                executable = Join-Path $env:LOCALAPPDATA 'Programs\Python\Launcher\py.exe'
                prefix = @('-3.12')
            }
            $pythonCandidates += @{
                executable = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'
                prefix = @()
            }
        }
        foreach ($registryPath in @(
            'HKCU:\Software\Python\PythonCore\3.12\InstallPath',
            'HKLM:\Software\Python\PythonCore\3.12\InstallPath',
            'HKLM:\Software\WOW6432Node\Python\PythonCore\3.12\InstallPath'
        )) {
            $registryValue = Get-ItemProperty -LiteralPath $registryPath -ErrorAction SilentlyContinue
            if ($null -ne $registryValue) {
                $registryExecutable = [string]$registryValue.ExecutablePath
                if ([string]::IsNullOrWhiteSpace($registryExecutable)) {
                    $registryInstall = [string]$registryValue.'(default)'
                    if (-not [string]::IsNullOrWhiteSpace($registryInstall)) {
                        $registryExecutable = Join-Path $registryInstall 'python.exe'
                    }
                }
                if (-not [string]::IsNullOrWhiteSpace($registryExecutable)) {
                    $pythonCandidates += @{
                        executable = $registryExecutable
                        prefix = @()
                    }
                }
            }
        }
        $pythonCommand = Get-Command 'python.exe' -ErrorAction SilentlyContinue
        if ($null -ne $pythonCommand) {
            $pythonCandidates += @{
                executable = [string]$pythonCommand.Source
                prefix = @()
            }
        }
        $pythonCandidates += @{
            executable = Join-Path $frameworkRoot '.venv\Scripts\python.exe'
            prefix = @()
        }
    }
    foreach ($candidate in $pythonCandidates) {
        $candidateExecutable = [string]$candidate.executable
        $candidatePrefix = @($candidate.prefix)
        if (
            (Test-Path -LiteralPath $candidateExecutable -PathType Leaf) -and
            (Test-Python312 -FilePath $candidateExecutable -Prefix $candidatePrefix)
        ) {
            $pythonExecutable = $candidateExecutable
            $pythonPrefix = $candidatePrefix
            break
        }
    }
    if ($null -eq $pythonExecutable) {
        if ($manifestPropertyNames -contains 'python_runtime') {
            $runtimeArchiveName = [string]$manifest.python_runtime.name
            if ($declaredFileNames -notcontains $runtimeArchiveName) {
                throw "Bundled Python runtime is not hash-bound by the release manifest: $runtimeArchiveName"
            }
            $runtimeArchive = Join-Path $wheelhousePath $runtimeArchiveName
            if (-not (Test-Path -LiteralPath $runtimeArchive -PathType Leaf)) {
                throw "Bundled Python runtime is missing: $runtimeArchive"
            }
            $bundledVersion = [string]$manifest.python_runtime.version
            if ($bundledVersion -notmatch '^3\.12\.\d+$') {
                throw "Bundled Python version is invalid: $bundledVersion"
            }
            $bundledPythonKind = 'nuget'
            $bundledPythonTarget = Get-FullPath (
                Join-Path $installRootPath "python-$bundledVersion"
            )
            $pythonExecutable = Join-Path $bundledPythonTarget 'tools\python.exe'
            if (Test-Path -LiteralPath $pythonExecutable -PathType Leaf) {
                if ($null -ne $priorInstallReceipt) {
                    $priorPropertyNames = @($priorInstallReceipt.PSObject.Properties.Name)
                    if (
                        $priorPropertyNames -contains 'bundled_python_owned' -and
                        $priorInstallReceipt.bundled_python_owned -eq $true -and
                        $priorPropertyNames -contains 'bundled_python_root' -and
                        (Get-FullPath ([string]$priorInstallReceipt.bundled_python_root)) -eq $bundledPythonTarget
                    ) {
                        $bundledPythonOwned = $true
                    }
                }
            }
            elseif (-not $ValidateOnly) {
                New-Item -ItemType Directory -Path $bundledPythonTarget -Force | Out-Null
                $bundledPythonInstalledNow = $true
                Add-Type -AssemblyName System.IO.Compression.FileSystem
                $archive = [System.IO.Compression.ZipFile]::OpenRead($runtimeArchive)
                try {
                    foreach ($entry in $archive.Entries) {
                        $entryTarget = Get-FullPath (Join-Path $bundledPythonTarget $entry.FullName)
                        if (
                            $entryTarget -ne $bundledPythonTarget -and
                            -not (Test-PathInside -Child $entryTarget -Parent $bundledPythonTarget)
                        ) {
                            throw "Bundled Python archive escapes its target: $($entry.FullName)"
                        }
                    }
                }
                finally {
                    $archive.Dispose()
                }
                [System.IO.Compression.ZipFile]::ExtractToDirectory(
                    $runtimeArchive,
                    $bundledPythonTarget
                )
                if (-not (Test-Path -LiteralPath $pythonExecutable -PathType Leaf)) {
                    throw 'Bundled Python runtime did not contain the expected executable.'
                }
                $installedSignature = Get-AuthenticodeSignature -LiteralPath $pythonExecutable
                if (
                    $installedSignature.Status -ne [System.Management.Automation.SignatureStatus]::Valid -or
                    $null -eq $installedSignature.SignerCertificate -or
                    $installedSignature.SignerCertificate.Subject -notmatch 'Python Software Foundation' -or
                    -not (Test-Python312 -FilePath $pythonExecutable)
                ) {
                    throw 'Bundled Python executable failed provenance or version verification.'
                }
                $bundledPythonOwned = $true
            }
        }
    }
}
if ($null -eq $pythonExecutable) {
    throw (
        "Python 3.12 was not found. Install Python 3.12 or pass " +
        "-Python C:\path\to\python.exe."
    )
}
$pythonVersionArguments = @(
    $pythonPrefix +
    @(
        '-I',
        '-c',
        (
            'import platform,sys;' +
            'print(platform.python_version());' +
            'raise SystemExit(0 if sys.version_info[:2] == (3,12) else 2)'
        )
    )
)
$pythonVersion = 'bundled-3.12'
$pythonSource = $pythonExecutable
if (-not ($ValidateOnly -and $null -ne $bundledPythonTarget -and -not (Test-Path -LiteralPath $pythonExecutable))) {
    $pythonVersion = & $pythonExecutable @pythonVersionArguments
    if ($LASTEXITCODE -ne 0) {
        throw "Python 3.12 is required. Selected executable: $pythonExecutable"
    }

    $pythonSource = & $pythonExecutable @(
        $pythonPrefix +
        @(
            '-I',
            '-c',
            'import pathlib,sys;print(pathlib.Path(sys.executable).resolve())'
        )
    )
    if ($LASTEXITCODE -ne 0) {
        throw "Cannot inspect selected Python executable: $pythonExecutable"
    }
}

Write-Output "Framework: $frameworkRoot"
Write-Output "Version: $version"
Write-Output "Python: $pythonVersion ($pythonSource)"
Write-Output "Git: $gitVersion ($gitExecutable)"
Write-Output "Wheelhouse: $wheelhousePath"
Write-Output "Venv: $venvPath"
Write-Output "Runtime: $runtimeRootPath"

if ($ValidateOnly) {
    Write-Output 'ARIA setup validation: OK'
    return
}

Assert-SafeVenvTarget `
    -Path $venvPath `
    -ExpectedLeaf $venvLeaf `
    -Framework $frameworkRoot `
    -Install $installRootPath

if ($Recreate -and (Test-Path -LiteralPath $venvPath)) {
    Remove-Item -LiteralPath $venvPath -Recurse -Force
}

$createdVenv = $false
$codexInstalledNow = $false
$shimCreated = $false
$userPathChanged = $false
$shimPath = $null
$ariaExe = $null
$previousUserPath = [Environment]::GetEnvironmentVariable(
    'Path',
    [EnvironmentVariableTarget]::User
)
$previousRuntime = [Environment]::GetEnvironmentVariable(
    'ARIA_RUNTIME_ROOT',
    [EnvironmentVariableTarget]::Process
)
$previousUtf8 = [Environment]::GetEnvironmentVariable(
    'PYTHONUTF8',
    [EnvironmentVariableTarget]::Process
)

try {
    New-Item -ItemType Directory -Path $installRootPath -Force | Out-Null
    if (-not (Test-Path -LiteralPath $venvPath -PathType Container)) {
        $createArguments = @($pythonPrefix + @('-m', 'venv', $venvPath))
        $createdVenv = $true
        Invoke-NativeChecked `
            -FilePath $pythonExecutable `
            -Arguments $createArguments `
            -Step 'Create ARIA venv'
    }

    $venvPython = Join-Path $venvPath 'Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
        throw "Venv is incomplete: $venvPython is missing. Run setup.ps1 -Recreate."
    }

    $venvVersion = & $venvPython '-I' '-c' (
        'import platform,sys;' +
        'print(platform.python_version());' +
        'raise SystemExit(0 if sys.version_info[:2] == (3,12) else 2)'
    )
    if ($LASTEXITCODE -ne 0) {
        throw "Existing venv uses Python $venvVersion instead of Python 3.12. Run setup.ps1 -Recreate."
    }

    Invoke-NativeChecked `
        -FilePath $venvPython `
        -Arguments @(
            '-m',
            'pip',
            'install',
            '--disable-pip-version-check',
            '--no-index',
            '--find-links',
            $wheelhousePath,
            '--force-reinstall',
            "aria-codex==$version"
        ) `
        -Step 'Install ARIA wheel offline'

    Invoke-NativeChecked `
        -FilePath $venvPython `
        -Arguments @('-m', 'pip', 'check') `
        -Step 'Check installed dependencies'

    $probeCode = (
        'import aria,importlib.metadata as m,json,pathlib,sys;' +
        'print(json.dumps(dict(' +
        "distribution=m.version('aria-codex')," +
        "module=getattr(aria,'__version__',None)," +
        'aria_file=str(pathlib.Path(aria.__file__).resolve()),' +
        'executable=str(pathlib.Path(sys.executable).resolve())' +
        ')))'
    )
    $probeOutput = @(& $venvPython '-I' '-c' $probeCode)
    if ($LASTEXITCODE -ne 0 -or $probeOutput.Count -lt 1) {
        throw 'Installed ARIA provenance check failed.'
    }
    $probe = $probeOutput[-1] | ConvertFrom-Json
    if ($probe.distribution -ne $version -or $probe.module -ne $version) {
        throw (
            "Installed ARIA version mismatch: " +
            "distribution=$($probe.distribution), module=$($probe.module), expected=$version."
        )
    }
    if (-not (Test-PathInside -Child $probe.aria_file -Parent $venvPath)) {
        throw "ARIA imports outside the new venv: $($probe.aria_file)"
    }
    if (-not (Test-PathInside -Child $probe.executable -Parent $venvPath)) {
        throw "Python executable is outside the new venv: $($probe.executable)"
    }

    New-Item -ItemType Directory -Path $runtimeRootPath -Force | Out-Null
    $env:ARIA_RUNTIME_ROOT = $runtimeRootPath
    $env:PYTHONUTF8 = '1'
    $ariaExe = Join-Path $venvPath 'Scripts\aria.exe'
    if (-not (Test-Path -LiteralPath $ariaExe -PathType Leaf)) {
        throw "ARIA console entry point is missing: $ariaExe"
    }
    Invoke-NativeChecked `
        -FilePath $ariaExe `
        -Arguments @('--framework-root', $frameworkRoot, 'doctor') `
        -Step 'Run ARIA framework doctor'

    $codexArguments = @('--framework-root', $frameworkRoot, 'codex', 'install')
    if (-not [string]::IsNullOrWhiteSpace($GithubClientId)) {
        $codexArguments += @(
            '--github-client-id',
            $GithubClientId,
            '--coordinator-integration-id',
            [string]$CoordinatorIntegrationId
        )
    }
    $codexOutput = @(& $ariaExe @codexArguments)
    if ($LASTEXITCODE -ne 0 -or $codexOutput.Count -lt 1) {
        throw 'Install ARIA Codex skill failed.'
    }
    $codexResult = ($codexOutput -join [Environment]::NewLine) | ConvertFrom-Json
    if ($codexResult.ok -ne $true) {
        throw 'ARIA Codex skill read-back failed.'
    }
    $codexInstalledNow = $codexResult.installed -eq $true

    $binPath = Get-FullPath (Join-Path $installRootPath 'bin')
    New-Item -ItemType Directory -Path $binPath -Force | Out-Null
    $shimPath = Join-Path $binPath 'aria.cmd'
    $shimLines = @(
        '@echo off',
        "set ""ARIA_RUNTIME_ROOT=$runtimeRootPath""",
        """$ariaExe"" --framework-root ""$frameworkRoot"" %*"
    )
    [System.IO.File]::WriteAllLines(
        $shimPath,
        $shimLines,
        [System.Text.UTF8Encoding]::new($false)
    )
    $shimCreated = $true
    if (-not (Test-Path -LiteralPath $shimPath -PathType Leaf)) {
        throw "ARIA command shim read-back failed: $shimPath"
    }

    if (-not $NoPathUpdate) {
        $userPathParts = @(
            ([string]$previousUserPath -split ';') |
                Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
        )
        $pathPresent = @($userPathParts | Where-Object {
            ([string]$_).TrimEnd('\', '/') -ieq $binPath.TrimEnd('\', '/')
        }).Count -gt 0
        if (-not $pathPresent) {
            $updatedUserPath = (@($userPathParts + $binPath) -join ';')
            [Environment]::SetEnvironmentVariable(
                'Path',
                $updatedUserPath,
                [EnvironmentVariableTarget]::User
            )
            $userPathChanged = $true
        }
    }

    $uninstallSource = Join-Path $frameworkRoot 'uninstall.ps1'
    $uninstallTarget = Join-Path $installRootPath 'uninstall.ps1'
    if (-not (Test-Path -LiteralPath $uninstallSource -PathType Leaf)) {
        throw "ARIA uninstaller is missing: $uninstallSource"
    }
    Copy-Item -LiteralPath $uninstallSource -Destination $uninstallTarget -Force
    $receipt = [ordered]@{
        schema_version = 1
        version = $version
        framework_root = $frameworkRoot
        install_root = $installRootPath
        runtime_root = $runtimeRootPath
        venv = $venvPath
        aria_executable = $ariaExe
        bin = $binPath
        shim = $shimPath
        bundled_python_owned = $bundledPythonOwned
        bundled_python_root = $bundledPythonTarget
        bundled_python_kind = $bundledPythonKind
        python_runtime_archive = if ($manifestPropertyNames -contains 'python_runtime') {
            Join-Path $wheelhousePath ([string]$manifest.python_runtime.name)
        } else { $null }
        python_runtime_sha256 = if ($manifestPropertyNames -contains 'python_runtime') {
            [string](@(
                $manifestFiles | Where-Object {
                    [string]$_.name -eq [string]$manifest.python_runtime.name
                }
            )[0].sha256)
        } else { $null }
        codex_skill_path = [string]$codexResult.path
        codex_skill_sha256 = [string]$codexResult.sha256
        codex_skill_owned = (
            $codexInstalledNow -or
            (
                $null -ne $priorInstallReceipt -and
                @($priorInstallReceipt.PSObject.Properties.Name) -contains 'codex_skill_owned' -and
                $priorInstallReceipt.codex_skill_owned -eq $true -and
                (Get-FullPath ([string]$priorInstallReceipt.codex_skill_path)) -eq
                    (Get-FullPath ([string]$codexResult.path))
            )
        )
        provider_profile_configured = $codexResult.provider_profile_configured -eq $true
        user_path_added = $userPathChanged
        uninstaller = $uninstallTarget
        installed_at = [DateTimeOffset]::UtcNow.ToString('o')
    }
    $receiptJson = $receipt | ConvertTo-Json -Depth 4
    [System.IO.File]::WriteAllText(
        $installReceiptPath,
        $receiptJson,
        [System.Text.UTF8Encoding]::new($false)
    )
    $receiptReadBack = Get-Content -LiteralPath $installReceiptPath -Raw | ConvertFrom-Json
    if (
        $receiptReadBack.version -ne $version -or
        $receiptReadBack.aria_executable -ne $ariaExe -or
        $receiptReadBack.codex_skill_sha256 -ne $codexResult.sha256
    ) {
        throw 'ARIA install receipt read-back failed.'
    }

    Write-Output 'ARIA setup: OK'
    Write-Output "Executable: $shimPath"
    Write-Output "Framework root: $frameworkRoot"
    Write-Output "Runtime root: $runtimeRootPath"
    Write-Output "Codex skill: $($codexResult.path)"
    Write-Output "Install receipt: $installReceiptPath"
}
catch {
    if ($userPathChanged) {
        [Environment]::SetEnvironmentVariable(
            'Path',
            $previousUserPath,
            [EnvironmentVariableTarget]::User
        )
    }
    if ($shimCreated -and $null -ne $shimPath -and (Test-Path -LiteralPath $shimPath -PathType Leaf)) {
        Remove-Item -LiteralPath $shimPath -Force -ErrorAction SilentlyContinue
    }
    if ($codexInstalledNow -and $null -ne $ariaExe -and (Test-Path -LiteralPath $ariaExe -PathType Leaf)) {
        & $ariaExe --framework-root $frameworkRoot codex remove | Out-Null
    }
    if ($createdVenv -and (Test-Path -LiteralPath $venvPath)) {
        Assert-SafeVenvTarget `
            -Path $venvPath `
            -ExpectedLeaf $venvLeaf `
            -Framework $frameworkRoot `
            -Install $installRootPath
        Remove-Item -LiteralPath $venvPath -Recurse -Force -ErrorAction SilentlyContinue
    }
    if ($bundledPythonInstalledNow -and $null -ne $bundledPythonTarget) {
        if ($bundledPythonKind -eq 'nuget') {
            if (Test-Path -LiteralPath $bundledPythonTarget -PathType Container) {
                Remove-Item -LiteralPath $bundledPythonTarget -Recurse -Force -ErrorAction SilentlyContinue
            }
        }
    }
    throw
}
finally {
    if ($null -eq $previousRuntime) {
        Remove-Item Env:ARIA_RUNTIME_ROOT -ErrorAction SilentlyContinue
    }
    else {
        $env:ARIA_RUNTIME_ROOT = $previousRuntime
    }
    if ($null -eq $previousUtf8) {
        Remove-Item Env:PYTHONUTF8 -ErrorAction SilentlyContinue
    }
    else {
        $env:PYTHONUTF8 = $previousUtf8
    }
}
