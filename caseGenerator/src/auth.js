import { toast, clipboardRead, clipboardWrite } from './utils.js';

const TOKEN_STORAGE_KEY = 'swebench_tokens';

// Token store — persisted to sessionStorage
const tokens = {
  management: '',
  storage: '',
  batch: '',
};

function persistTokens() {
  try { sessionStorage.setItem(TOKEN_STORAGE_KEY, JSON.stringify(tokens)); } catch {}
}

function restoreTokens() {
  try {
    const raw = sessionStorage.getItem(TOKEN_STORAGE_KEY);
    if (!raw) return;
    const saved = JSON.parse(raw);
    if (saved.management) tokens.management = saved.management;
    if (saved.storage) tokens.storage = saved.storage;
    if (saved.batch) tokens.batch = saved.batch;
  } catch {}
}

export function getToken(resource) {
  const t = tokens[resource];
  if (!t) {
    const labels = { management: 'Azure Management', storage: 'Blob Storage', batch: 'Batch' };
    throw new Error(`No ${labels[resource] || resource} token — click "🔑 Refresh Tokens" in Settings`);
  }
  return t;
}

export function setTokens(obj) {
  if (obj.management) tokens.management = obj.management;
  if (obj.storage) tokens.storage = obj.storage;
  if (obj.batch) tokens.batch = obj.batch;
  persistTokens();
  updateTokenUI();
}

export function hasAllTokens() {
  return !!(tokens.management && tokens.storage && tokens.batch);
}

/**
 * Import tokens from clipboard. Accepts either:
 *   - JSON: { "management": "...", "storage": "...", "batch": "..." }
 *   - The output of the get_tokens.ps1 script
 */
export async function importTokens(pastedText) {
  try {
    let text = pastedText;
    if (!text) {
      try { text = await clipboardRead(); } catch {
        // Clipboard API unavailable (HTTP) — show paste dialog
        showPasteDialog();
        return;
      }
    }
    if (!text?.trim()) { toast('Clipboard is empty'); return; }
    let data;
    try { data = JSON.parse(text); } catch { toast('Clipboard is not valid JSON'); return; }

    // Support both new key names and legacy field names
    const mgmt = data.management || data.azToken || '';
    const stor = data.storage || data.azBlobToken || '';
    const bat = data.batch || data.azBatchToken || '';

    let count = 0;
    if (mgmt) { tokens.management = mgmt; count++; }
    if (stor) { tokens.storage = stor; count++; }
    if (bat) { tokens.batch = bat; count++; }

    // Also import batch endpoint if present
    if (data.azBatchEndpoint || data.batchEndpoint) {
      const el = document.getElementById('azBatchEndpoint');
      if (el) el.value = data.azBatchEndpoint || data.batchEndpoint;
    }

    if (count > 0) {
      persistTokens();
      toast(`Imported ${count} token(s)`);
      updateTokenUI();
    } else {
      toast('No tokens found in JSON — expected keys: management, storage, batch');
    }
  } catch (e) { toast('Clipboard: ' + e.message); }
}

function showPasteDialog() {
  const existing = document.getElementById('pasteDialog');
  if (existing) existing.remove();
  const d = document.createElement('div');
  d.id = 'pasteDialog';
  d.style.cssText = 'position:fixed;inset:0;z-index:9999;background:rgba(0,0,0,.5);display:flex;align-items:center;justify-content:center';
  d.innerHTML = `<div style="background:var(--bg,#1e1e2e);border-radius:12px;padding:24px;width:480px;max-width:90vw;box-shadow:0 8px 32px rgba(0,0,0,.4)">
    <h3 style="margin:0 0 8px;color:var(--fg,#cdd6f4)">Paste Token JSON</h3>
    <p style="margin:0 0 12px;font-size:12px;color:var(--muted,#6c7086)">Clipboard API is not available on HTTP. Paste the JSON from get_tokens.ps1 here:</p>
    <textarea id="pasteTokenArea" rows="4" style="width:100%;box-sizing:border-box;background:var(--surface,#313244);color:var(--fg,#cdd6f4);border:1px solid var(--border,#45475a);border-radius:6px;padding:8px;font-family:monospace;font-size:12px;resize:vertical" placeholder='{"management":"...","storage":"...","batch":"..."}'></textarea>
    <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:12px">
      <button onclick="document.getElementById('pasteDialog').remove()" style="padding:6px 16px;border-radius:6px;border:1px solid var(--border,#45475a);background:transparent;color:var(--fg,#cdd6f4);cursor:pointer">Cancel</button>
      <button onclick="window._app.importTokens(document.getElementById('pasteTokenArea').value);document.getElementById('pasteDialog').remove()" style="padding:6px 16px;border-radius:6px;border:none;background:var(--accent,#89b4fa);color:#1e1e2e;cursor:pointer;font-weight:600">Import</button>
    </div>
  </div>`;
  document.body.appendChild(d);
  d.addEventListener('click', e => { if (e.target === d) d.remove(); });
  setTimeout(() => document.getElementById('pasteTokenArea')?.focus(), 50);
}

export async function refreshTokensViaScript() {
  const script = generatePowerShellScript();
  await clipboardWrite(script);
  toast('PowerShell script copied! Paste in terminal, then click "📋 Import from Clipboard"');
}

function generatePowerShellScript() {
  return `# Run this in PowerShell, then click "📋 Import from Clipboard" in the UI
$mgmt = az account get-access-token --resource https://management.azure.com --query accessToken -o tsv
$storage = az account get-access-token --resource https://storage.azure.com --query accessToken -o tsv
$batch = az account get-access-token --resource https://batch.core.windows.net --query accessToken -o tsv
$json = @{ management = $mgmt; storage = $storage; batch = $batch } | ConvertTo-Json -Compress
Set-Clipboard $json
Write-Host "✅ Tokens copied to clipboard — go back to the browser and click Import" -ForegroundColor Green`;
}

function updateTokenUI() {
  const el = document.getElementById('tokenStatus');
  if (!el) return;
  const items = [
    { key: 'management', label: 'Mgmt' },
    { key: 'storage', label: 'Storage' },
    { key: 'batch', label: 'Batch' },
  ];
  el.innerHTML = items.map(({ key, label }) => {
    const ok = !!tokens[key];
    return `<span style="color:${ok ? 'var(--green)' : 'var(--muted)'};font-size:11px;font-weight:600">${ok ? '✓' : '○'} ${label}</span>`;
  }).join(' ');
}

export async function initAuth() {
  restoreTokens();
  updateTokenUI();
}

export async function signIn() { await refreshTokensViaScript(); }
export async function signOut() {
  tokens.management = ''; tokens.storage = ''; tokens.batch = '';
  persistTokens();
  updateTokenUI();
  toast('Tokens cleared');
}
