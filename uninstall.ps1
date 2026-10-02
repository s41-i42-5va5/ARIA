[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'High')]
param(
    [string]$InstallRoot,
    [switch]$ForceCodexSkill,
    [switch]$KeepExecutables
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-FullPath {
    param([Parameter(Mandatory = $true)][string]$Path)
    return [System.IO.Path]::GetFullPath($Path)
}

function Test-PathInside {
    param(
        [Parameter(Mandatory = $true)][string]$Child,
        [Parameter(Mandatory = $true)][string]$Parent
    )
    $childPath = (Get-FullPath $Child).TrimEnd('\', '/')
    $parentPath = (Get-FullPath $Parent).TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
    return $childPath.StartsWith($parentPath, [System.StringComparison]::OrdinalIgnoreCase)
}

if ([string]::IsNullOrWhiteSpace($InstallRoot)) {
    $InstallRoot = $PSScriptRoot
}
$installRootPath = Get-FullPath $InstallRoot
$rootPath = [System.IO.Path]::GetPathRoot($installRootPath)
if ($installRootPath -eq $rootPath -or (Split-Path -Leaf $installRootPath) -eq '') {
    throw "Unsafe ARIA install root: $installRootPath"
}
$receiptPath = Join-Path $installRootPath 'install-receipt.json'
if (-not (Test-Path -LiteralPath $receiptPath -PathType Leaf)) {
    throw "ARIA install receipt is missing: $receiptPath"
}
$receipt = Get-Content -LiteralPath $receiptPath -Raw | ConvertFrom-Json
if (
    $receipt.schema_version -ne 1 -or
    $receipt.version -ne '1.5.5' -or
    (Get-FullPath ([string]$receipt.install_root)) -ne $installRootPath
) {
    throw 'ARIA install receipt is invalid or belongs to another install root.'
}

$venvPath = Get-FullPath ([string]$receipt.venv)
$binPath = Get-FullPath ([string]$receipt.bin)
$ariaExe = Get-FullPath ([string]$receipt.aria_executable)
$frameworkRoot = Get-FullPath ([string]$receipt.framework_root)
$runtimeRoot = Get-FullPath ([string]$receipt.runtime_root)
foreach ($target in @($venvPath, $binPath)) {
    if (-not (Test-PathInside -Child $target -Parent $installRootPath)) {
        throw "Install receipt target escapes ARIA install root: $target"
    }
}
if ($runtimeRoot -eq $installRootPath -or (Test-PathInside -Child $runtimeRoot -Parent $installRootPath)) {
    throw 'ARIA runtime must be preserved outside the executable install root.'
}

if (-not $PSCmdlet.ShouldProcess($installRootPath, 'Uninstall ARIA executables and Codex skill while preserving projects and runtime')) {
    return
}

$previousRuntime = [Environment]::GetEnvironmentVariable(
    'ARIA_RUNTIME_ROOT',
    [EnvironmentVariableTarget]::Process
)
try {
    $env:ARIA_RUNTIME_ROOT = $runtimeRoot
    if (Test-Path -LiteralPath $ariaExe -PathType Leaf) {
        $projectsOutput = @(& $ariaExe --framework-root $frameworkRoot projects)
        if ($projectsOutput.Count -gt 0) {
            $projects = ($projectsOutput -join [Environment]::NewLine) | ConvertFrom-Json
            foreach ($project in @($projects.projects)) {
                & $ariaExe --framework-root $frameworkRoot coordinator remove --project $project.project_id | Out-Null
                if ($LASTEXITCODE -ne 0) {
                    throw "Cannot remove coordinator task for project $($project.project_id)."
                }
            }
        }
        $receiptPropertyNames = @($receipt.PSObject.Properties.Name)
        if (
            $receiptPropertyNames -contains 'codex_skill_owned' -and
            $receipt.codex_skill_owned -eq $true
        ) {
            $removeArgs = @('--framework-root', $frameworkRoot, 'codex', 'remove')
            if ($ForceCodexSkill) {
                $removeArgs += '--force'
            }
            & $ariaExe @removeArgs | Out-Null
            if ($LASTEXITCODE -ne 0) {
                throw 'Cannot safely remove the ARIA Codex skill.'
            }
        }
    }

    if ($receipt.user_path_added -eq $true) {
        $userPath = [Environment]::GetEnvironmentVariable(
            'Path',
            [EnvironmentVariableTarget]::User
        )
        $remaining = @(
            ([string]$userPath -split ';') |
                Where-Object {
                    -not [string]::IsNullOrWhiteSpace($_) -and
                    ([string]$_).TrimEnd('\', '/') -ine $binPath.TrimEnd('\', '/')
                }
        )
        [Environment]::SetEnvironmentVariable(
            'Path',
            ($remaining -join ';'),
            [EnvironmentVariableTarget]::User
        )
    }

    if (-not $KeepExecutables) {
        if (Test-Path -LiteralPath $venvPath -PathType Container) {
            Remove-Item -LiteralPath $venvPath -Recurse -Force
        }
        if (Test-Path -LiteralPath $binPath -PathType Container) {
            Remove-Item -LiteralPath $binPath -Recurse -Force
        }
        if ($receipt.bundled_python_owned -eq $true -and $null -ne $receipt.bundled_python_root) {
            $pythonRoot = Get-FullPath ([string]$receipt.bundled_python_root)
            if (-not (Test-PathInside -Child $pythonRoot -Parent $installRootPath)) {
                throw "Bundled Python target escapes ARIA install root: $pythonRoot"
            }
            $receiptPropertyNames = @($receipt.PSObject.Properties.Name)
            $pythonKind = if ($receiptPropertyNames -contains 'bundled_python_kind') {
                [string]$receipt.bundled_python_kind
            } else { '' }
            if ($pythonKind -eq 'nuget') {
                $pythonExecutable = Join-Path $pythonRoot 'tools\python.exe'
                if (-not (Test-Path -LiteralPath $pythonExecutable -PathType Leaf)) {
                    throw 'Owned bundled Python executable is missing; refusing recursive removal.'
                }
                $pythonSignature = Get-AuthenticodeSignature -LiteralPath $pythonExecutable
                if (
                    $pythonSignature.Status -ne [System.Management.Automation.SignatureStatus]::Valid -or
                    $null -eq $pythonSignature.SignerCertificate -or
                    $pythonSignature.SignerCertificate.Subject -notmatch 'Python Software Foundation'
                ) {
                    throw 'Owned bundled Python executable has invalid provenance.'
                }
                Remove-Item -LiteralPath $pythonRoot -Recurse -Force
            }
            else {
                throw "Unsupported bundled Python kind: $pythonKind"
            }
        }
    }

    $completedReceipt = [ordered]@{
        schema_version = 1
        version = '1.5.5'
        uninstalled_at = [DateTimeOffset]::UtcNow.ToString('o')
        projects_preserved = $true
        runtime_preserved = $true
        provider_profile_preserved = $true
        install_root = $installRootPath
        runtime_root = $runtimeRoot
    }
    $completedPath = Join-Path $installRootPath 'uninstall-receipt.json'
    [System.IO.File]::WriteAllText(
        $completedPath,
        ($completedReceipt | ConvertTo-Json -Depth 3),
        [System.Text.UTF8Encoding]::new($false)
    )
    Remove-Item -LiteralPath $receiptPath -Force
    Write-Output 'ARIA uninstall: OK'
    Write-Output "Projects preserved: true"
    Write-Output "Runtime preserved: $runtimeRoot"
    Write-Output "Receipt: $completedPath"
}
finally {
    if ($null -eq $previousRuntime) {
        Remove-Item Env:ARIA_RUNTIME_ROOT -ErrorAction SilentlyContinue
    }
    else {
        $env:ARIA_RUNTIME_ROOT = $previousRuntime
    }
}
