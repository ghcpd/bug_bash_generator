# ============================================================================
# get_tokens.ps1 — Fetch all Azure tokens → copy JSON to clipboard
# ============================================================================
# Usage:   .\scripts\generate_case\auth\get_tokens.ps1
#          .\scripts\generate_case\auth\get_tokens.ps1 -BatchAccount myaccount
#
# What it does:
#   1. Fetches Management / Storage / Batch tokens via az CLI
#   2. Auto-detects Batch endpoint from ADF linked service
#   3. Copies JSON to clipboard — use "Import from Clipboard" in HTML
#
# Prerequisites: az login (already authenticated)
# ============================================================================

param(
    [string]$BatchAccount = "",
    [string]$ResourceGroup = "acv-dp-wu2-p-001-rg",
    [string]$FactoryName = "acv-dp-wu2-p-001-adf",
    [string]$BatchLinkedService = "gen_rubric"
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host "`n  SWE-bench Token Loader" -ForegroundColor Cyan
Write-Host "  =====================`n" -ForegroundColor DarkCyan

# 1. Management token (for ADF API)
Write-Host "  [1/4] Management token..." -NoNewline
$mgmtToken = az account get-access-token --resource https://management.azure.com --query accessToken -o tsv
Write-Host " OK" -ForegroundColor Green

# 2. Storage token (for Blob results)
Write-Host "  [2/4] Storage token..." -NoNewline
$storageToken = az account get-access-token --resource https://storage.azure.com --query accessToken -o tsv
Write-Host " OK" -ForegroundColor Green

# 3. Batch token
Write-Host "  [3/4] Batch token..." -NoNewline
$batchToken = az account get-access-token --resource https://batch.core.windows.net --query accessToken -o tsv
Write-Host " OK" -ForegroundColor Green

# 4. Batch endpoint — try to auto-detect from ADF linked service or direct param
$batchEndpoint = ""
if ($BatchAccount) {
    Write-Host "  [4/4] Batch endpoint (from account name)..." -NoNewline
    $batchEndpoint = az batch account show --name $BatchAccount --resource-group $ResourceGroup --query accountEndpoint -o tsv 2>$null
    Write-Host " OK" -ForegroundColor Green
} else {
    Write-Host "  [4/4] Batch endpoint (from ADF linked service '$BatchLinkedService')..." -NoNewline
    try {
        $lsJson = az datafactory linked-service show `
            --factory-name $FactoryName `
            --resource-group $ResourceGroup `
            --name $BatchLinkedService `
            --query properties -o json 2>$null
        if ($lsJson) {
            $ls = $lsJson | ConvertFrom-Json
            if ($ls.batchUri) {
                $batchEndpoint = $ls.batchUri -replace "^https://", ""
            } elseif ($ls.accountEndpoint) {
                $batchEndpoint = $ls.accountEndpoint
            } elseif ($ls.accountName) {
                $batchEndpoint = "$($ls.accountName).westus2.batch.azure.com"
            }
        }
        if ($batchEndpoint) {
            Write-Host " OK" -ForegroundColor Green
        } else {
            Write-Host " SKIP (use -BatchAccount)" -ForegroundColor Yellow
        }
    } catch {
        Write-Host " SKIP" -ForegroundColor Yellow
    }
}

# Build JSON payload
$result = @{
    azToken      = $mgmtToken
    azBlobToken  = $storageToken
    azBatchToken = $batchToken
}
if ($batchEndpoint) {
    $result.azBatchEndpoint = $batchEndpoint -replace "^https://", ""
}
$json = $result | ConvertTo-Json -Compress

$json | Set-Clipboard

Write-Host ""
Write-Host "  Tokens copied to clipboard!" -ForegroundColor Green
Write-Host "  Open auth\\prompt_generator.html and click 'Import Tokens from Clipboard'.`n" -ForegroundColor Cyan
