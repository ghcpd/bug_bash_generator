import { toast, esc, fmtBytes, fmtTime } from './utils.js';
import { blobBaseUrl, blobHeaders, corsErrorHtml } from './azure.js';
import { openModal } from './modal.js';
import { renderReview } from './review.js';

// Cache the last loaded JSONL text for tab switching
let _lastPreviewText = '';
let _lastPreviewName = '';

export async function refreshBlobs() {
  const el = document.getElementById('blobContent'), preview = document.getElementById('blobPreview');
  preview.innerHTML = ''; el.innerHTML = '<p class="empty"><span class="spin"></span> Listing blobs…</p>';
  try {
    const folder = document.getElementById('azOutputFolder').value.trim();
    const sub = document.getElementById('blobSubfolder').value;
    const prefix = folder + '/' + sub + '/';
    const url = blobBaseUrl() + `?restype=container&comp=list&prefix=${encodeURIComponent(prefix)}&delimiter=`;
    const resp = await fetch(url, { headers: blobHeaders() });
    if (!resp.ok) { let m = `HTTP ${resp.status}`; try { m = await resp.text(); } catch {} throw new Error(m); }
    const xml = await resp.text(); const parser = new DOMParser(); const doc = parser.parseFromString(xml, 'text/xml');
    const blobs = doc.querySelectorAll('Blob');
    if (!blobs.length) { el.innerHTML = '<p class="empty">No files found in ' + esc(prefix) + '</p>'; return; }
    const items = [];
    blobs.forEach(b => {
      const name = b.querySelector('Name')?.textContent || '';
      const size = parseInt(b.querySelector('Content-Length')?.textContent || '0');
      const modified = b.querySelector('Last-Modified')?.textContent || '';
      items.push({ name, size, modified, shortName: name.replace(prefix, '') });
    });
    items.sort((a, b) => new Date(b.modified) - new Date(a.modified));

    let h = `<div style="font-size:11px;color:var(--muted);padding:6px 0 8px">${items.length} files in <code>${esc(prefix)}</code></div>`;
    for (const f of items) {
      const isJsonl = f.shortName.endsWith('.jsonl');
      const safeN = f.name.replace(/'/g, "\\'");
      h += `<div class="blob-file" data-blob="${esc(f.name)}" ${isJsonl ? `style="cursor:pointer" onclick="window._app.previewBlob('${safeN}',this)"` : ''}>
        <span class="blob-icon">${isJsonl ? '📋' : '📦'}</span>
        <span class="blob-name">${esc(f.shortName)}</span>
        <span class="blob-size">${fmtBytes(f.size)}</span>
        <span class="blob-size">${fmtTime(f.modified)}</span>
        <button class="btn-copy" onclick="event.stopPropagation();window._app.copyText('${safeN}','File path')">📋</button>
        ${isJsonl ? `<button class="btn-sm" onclick="event.stopPropagation();window._app.previewBlob('${safeN}',this.closest('.blob-file'))">View</button>` : ''}
      </div>`;
    }
    el.innerHTML = h;
  } catch (e) { el.innerHTML = `<p class="empty" style="color:var(--red)">${corsErrorHtml(e.message)}</p>`; }
}

export async function previewBlob(blobName, triggerEl) {
  // Remove any existing inline preview
  const existing = document.getElementById('blobPreviewInline');
  if (existing) existing.remove();

  // Create inline preview container right after the clicked file row
  const previewDiv = document.createElement('div');
  previewDiv.id = 'blobPreviewInline';
  previewDiv.innerHTML = `<div class="blob-preview"><span class="spin"></span> Loading…</div>`;

  if (triggerEl) {
    triggerEl.insertAdjacentElement('afterend', previewDiv);
  } else {
    document.getElementById('blobPreview').appendChild(previewDiv);
  }

  try {
    const url = blobBaseUrl() + '/' + encodeURIComponent(blobName).replace(/%2F/g, '/');
    const resp = await fetch(url, { headers: blobHeaders() });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const text = await resp.text();
    _lastPreviewText = text;
    _lastPreviewName = blobName;

    if (blobName.endsWith('.jsonl')) {
      showPreviewWithTabs(blobName, text, previewDiv, 'raw');
    } else {
      previewDiv.innerHTML = `<div class="blob-preview">
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
          <span style="font-weight:600;font-size:12px;color:var(--accent);flex:1">${esc(blobName.split('/').pop())}</span>
          <button class="btn-copy" onclick="window._app.clipboardWrite(document.getElementById('blobText').textContent).then(()=>window._app.toast('Content copied!'))">📋 Copy</button>
          <button class="btn-sm" onclick="window._app.openModal('${esc(blobName.split('/').pop())}',document.getElementById('blobText').textContent)">⤢ Expand</button>
        </div>
        <pre id="blobText">${esc(text)}</pre>
      </div>`;
    }
  } catch (e) { previewDiv.innerHTML = `<div class="blob-preview"><p style="color:var(--red)">${corsErrorHtml(e.message)}</p></div>`; }
}

function showPreviewWithTabs(blobName, text, el, activeTab) {
  const shortName = blobName.split('/').pop();
  let display = text;
  try { display = text.trim().split('\n').map(line => JSON.stringify(JSON.parse(line), null, 2)).join('\n\n---\n\n'); } catch {}

  let h = `<div class="blob-preview" style="max-height:none">
    <div style="display:flex;align-items:center;gap:8px;margin-bottom:0">
      <span style="font-weight:600;font-size:12px;color:var(--accent);flex:1">${esc(shortName)}</span>
      <button class="btn-copy" onclick="window._app.clipboardWrite(document.getElementById('blobText')?.textContent||'').then(()=>window._app.toast('Content copied!'))">📋 Copy</button>
      <button class="btn-sm" onclick="window._app.openModal('${esc(shortName)}',document.getElementById('blobText')?.textContent||'')">⤢ Expand</button>
    </div>
    <div class="rv-tabs">
      <button class="rv-tab ${activeTab === 'raw' ? 'active' : ''}" onclick="window._app.switchPreviewTab('raw')">Raw</button>
      <button class="rv-tab ${activeTab === 'review' ? 'active' : ''}" onclick="window._app.switchPreviewTab('review')">🔍 Review</button>
    </div>
    <div id="previewTabContent">`;

  if (activeTab === 'raw') {
    h += `<pre id="blobText" style="max-height:400px;overflow:auto">${esc(display)}</pre>`;
  } else {
    h += `<div id="reviewContainer"></div>`;
  }
  h += `</div></div>`;
  el.innerHTML = h;

  if (activeTab === 'review') {
    renderReview(text, document.getElementById('reviewContainer'));
  }
}

export function switchPreviewTab(tab) {
  const el = document.getElementById('blobPreviewInline');
  if (!_lastPreviewText || !el) return;
  showPreviewWithTabs(_lastPreviewName, _lastPreviewText, el, tab);
}

export function switchResultsTab(tab) {
  const blobs = document.getElementById('resultsSubBlobs');
  const quality = document.getElementById('resultsSubQuality');
  if (!blobs || !quality) return;
  blobs.style.display = tab === 'blobs' ? '' : 'none';
  quality.style.display = tab === 'quality' ? '' : 'none';
  document.querySelectorAll('.qe-sub-tab').forEach((el, i) => {
    el.classList.toggle('active', (i === 0 && tab === 'blobs') || (i === 1 && tab === 'quality'));
  });
}
