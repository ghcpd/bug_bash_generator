# ============================================================================
# token_server.ps1 — Local token server for auth/prompt_generator.html
# ============================================================================
# Usage:   .\scripts\generate_case\auth\token_server.ps1
#
# Starts a tiny HTTP server on localhost:18923.
# The HTML page's "Fetch Tokens" button calls this server →
# server runs `az` CLI → returns tokens as JSON → page auto-fills.
#
# Keep this running in a terminal. Press Ctrl+C to stop.
# ============================================================================

param(
    [int]$Port = 18923,
    [string]$ResourceGroup = "acv-dp-wu2-p-001-rg",
    [string]$FactoryName = "acv-dp-wu2-p-001-adf",
    [string]$BatchLinkedService = "gen_rubric"
)

$ErrorActionPreference = "Stop"
$listener = [System.Net.HttpListener]::new()
$listener.Prefixes.Add("http://localhost:$Port/")
$listener.Start()

Write-Host ""
Write-Host "  Token Server running on http://localhost:$Port" -ForegroundColor Green
Write-Host "  Open auth\\prompt_generator.html and click [Fetch Tokens]" -ForegroundColor Cyan
Write-Host "  Press Ctrl+C to stop" -ForegroundColor DarkGray
Write-Host ""

try {
    while ($listener.IsListening) {
        $ctx = $listener.GetContext()
        $req = $ctx.Request
        $resp = $ctx.Response

        # CORS headers (allow file:// and any origin for local dev)
        $resp.Headers.Add("Access-Control-Allow-Origin", "*")
        $resp.Headers.Add("Access-Control-Allow-Methods", "GET, OPTIONS")
        $resp.Headers.Add("Access-Control-Allow-Headers", "Content-Type")

        # Handle preflight
        if ($req.HttpMethod -eq "OPTIONS") {
            $resp.StatusCode = 204
            $resp.Close()
            continue
        }

        if ($req.Url.AbsolutePath -eq "/tokens" -and $req.HttpMethod -eq "GET") {
            Write-Host "  [$(Get-Date -Format 'HH:mm:ss')] Fetching tokens..." -ForegroundColor Cyan

            try {
                $mgmt = az account get-access-token --resource https://management.azure.com --query accessToken -o tsv
                $storage = az account get-access-token --resource https://storage.azure.com --query accessToken -o tsv
                $batch = az account get-access-token --resource https://batch.core.windows.net --query accessToken -o tsv

                # Try to get batch endpoint
                $batchEp = ""
                try {
                    $lsJson = az datafactory linked-service show `
                        --factory-name $FactoryName `
                        --resource-group $ResourceGroup `
                        --name $BatchLinkedService `
                        --query properties.typeProperties -o json 2>$null
                    if ($lsJson) {
                        $ls = $lsJson | ConvertFrom-Json
                        $batchEp = $ls.accountEndpoint
                        if (-not $batchEp -and $ls.batchUri) {
                            $batchEp = $ls.batchUri -replace "^https://", ""
                        }
                    }
                } catch {}

                $result = @{
                    ok           = $true
                    azToken      = $mgmt
                    azBlobToken  = $storage
                    azBatchToken = $batch
                }
                if ($batchEp) { $result.azBatchEndpoint = $batchEp -replace "^https://", "" }

                $json = $result | ConvertTo-Json -Compress
                $bytes = [System.Text.Encoding]::UTF8.GetBytes($json)
                $resp.ContentType = "application/json"
                $resp.StatusCode = 200
                $resp.OutputStream.Write($bytes, 0, $bytes.Length)
                Write-Host "  [$(Get-Date -Format 'HH:mm:ss')] Tokens served OK" -ForegroundColor Green

            } catch {
                $errJson = @{ ok = $false; error = $_.Exception.Message } | ConvertTo-Json -Compress
                $bytes = [System.Text.Encoding]::UTF8.GetBytes($errJson)
                $resp.ContentType = "application/json"
                $resp.StatusCode = 500
                $resp.OutputStream.Write($bytes, 0, $bytes.Length)
                Write-Host "  [$(Get-Date -Format 'HH:mm:ss')] ERROR: $($_.Exception.Message)" -ForegroundColor Red
            }

        } elseif ($req.Url.AbsolutePath -eq "/health") {
            $bytes = [System.Text.Encoding]::UTF8.GetBytes('{"ok":true}')
            $resp.ContentType = "application/json"
            $resp.StatusCode = 200
            $resp.OutputStream.Write($bytes, 0, $bytes.Length)

        } else {
            $resp.StatusCode = 404
        }

        $resp.Close()
    }
} finally {
    $listener.Stop()
    Write-Host "`n  Server stopped." -ForegroundColor Yellow
}
