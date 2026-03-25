import { toast, esc } from './utils.js';
import { getToken } from './auth.js';

/* ==================== Azure Management (ADF) ==================== */

export function azBaseUrl() {
  const sub = document.getElementById('azSub').value.trim(),
    rg = document.getElementById('azRg').value.trim(),
    f = document.getElementById('azFactory').value.trim();
  return `https://management.azure.com/subscriptions/${encodeURIComponent(sub)}/resourceGroups/${encodeURIComponent(rg)}/providers/Microsoft.DataFactory/factories/${encodeURIComponent(f)}`;
}

function azHeaders() {
  const t = getToken('management');
  return { 'Authorization': 'Bearer ' + t, 'Content-Type': 'application/json' };
}

export async function azFetch(method, path, body) {
  const r = await fetch(azBaseUrl() + path, { method, headers: azHeaders(), body: body ? JSON.stringify(body) : undefined });
  if (!r.ok) { let m = `HTTP ${r.status}`; try { const e = await r.json(); m = e.error?.message || e.message || m; } catch {} throw new Error(m); }
  const t = await r.text();
  return t ? JSON.parse(t) : {};
}

/* ==================== Blob Storage ==================== */

export function blobBaseUrl() {
  return `https://${document.getElementById('azStorage').value.trim()}.blob.core.windows.net/${document.getElementById('azContainer').value.trim()}`;
}

export function blobStorageUrl() {
  return `https://${document.getElementById('azStorage').value.trim()}.blob.core.windows.net`;
}

export function blobHeaders() {
  const t = getToken('storage');
  return { 'Authorization': 'Bearer ' + t, 'x-ms-version': '2020-10-02' };
}

export function corsErrorHtml(msg) {
  const isCors = (msg || '').match(/ERR_FAILED|NetworkError|Failed to fetch/i);
  return isCors
    ? 'CORS error: Storage account blocks browser requests. Go to <b>Configure → Settings</b> and click <b>Enable CORS on Storage</b>, then retry.'
    : 'Error: ' + esc(msg);
}

export async function setCorsOnStorage() {
  const storage = document.getElementById('azStorage').value.trim();
  if (!storage) { toast('Fill in Storage Account first'); return; }
  try {
    const storageToken = getToken('storage');
    const url = `https://${storage}.blob.core.windows.net/?restype=service&comp=properties`;

    // Build CORS XML
    const corsXml = `<?xml version="1.0" encoding="utf-8"?>
<StorageServiceProperties>
  <Cors>
    <CorsRule>
      <AllowedOrigins>*</AllowedOrigins>
      <AllowedMethods>GET,HEAD,PUT,OPTIONS</AllowedMethods>
      <AllowedHeaders>Authorization,x-ms-version,x-ms-date,x-ms-blob-type,Content-Type</AllowedHeaders>
      <ExposedHeaders>x-ms-meta-*,Content-Length,Content-Type</ExposedHeaders>
      <MaxAgeInSeconds>3600</MaxAgeInSeconds>
    </CorsRule>
  </Cors>
</StorageServiceProperties>`;

    const resp = await fetch(url, {
      method: 'PUT',
      headers: {
        'Authorization': 'Bearer ' + storageToken,
        'x-ms-version': '2020-10-02',
        'Content-Type': 'application/xml',
      },
      body: corsXml,
    });
    if (!resp.ok) { let m = 'HTTP ' + resp.status; try { m = await resp.text(); } catch {} throw new Error(m); }
    toast('CORS enabled on ' + storage + '! Retry your request.');
  } catch (e) { toast('CORS setup failed: ' + e.message); }
}

/* ==================== Azure Batch ==================== */

export function batchEndpoint() {
  const ep = document.getElementById('azBatchEndpoint').value.trim();
  if (!ep) throw new Error('Set Batch Account Endpoint in Settings');
  return ep.replace(/^https?:\/\//, '').replace(/\/+$/, '');
}

function needsBatchProxy() {
  const h = location.hostname;
  return h !== 'localhost' && h !== '127.0.0.1' && !location.protocol.startsWith('file');
}

function batchHeaders() {
  const t = getToken('batch');
  const hdrs = { 'Authorization': 'Bearer ' + t, 'Content-Type': 'application/json', 'ocp-date': new Date().toUTCString() };
  if (needsBatchProxy()) hdrs['X-Batch-Host'] = batchEndpoint();
  return hdrs;
}

function batchProxyBase() {
  const base = (import.meta.env.BASE_URL || '/').replace(/\/+$/, '');
  return base + '/api/batch';
}

export async function batchFetch(pathOrUrl) {
  let url;
  const proxy = needsBatchProxy();
  if (pathOrUrl.startsWith('https://')) {
    // Absolute URL — extract host and path for proxy
    if (proxy) {
      const u = new URL(pathOrUrl);
      if (!u.search.includes('api-version=')) u.searchParams.set('api-version', '2024-02-01.19.0');
      url = batchProxyBase() + u.pathname + u.search;
    } else {
      url = pathOrUrl;
      if (!url.includes('api-version=')) url += (url.includes('?') ? '&' : '?') + 'api-version=2024-02-01.19.0';
    }
  } else {
    const sep = pathOrUrl.includes('?') ? '&' : '?';
    if (proxy) {
      url = batchProxyBase() + pathOrUrl + sep + 'api-version=2024-02-01.19.0';
    } else {
      url = 'https://' + batchEndpoint() + pathOrUrl + sep + 'api-version=2024-02-01.19.0';
    }
  }
  const hdrs = batchHeaders();
  const r = await fetch(url, { headers: hdrs });
  if (!r.ok) { let m = `HTTP ${r.status}`; try { const e = await r.json(); m = e.message?.value || e.code || m; } catch {} throw new Error(m); }
  return r;
}
