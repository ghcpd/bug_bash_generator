import { Chart, ArcElement, BarElement, BarController, DoughnutController, CategoryScale, LinearScale, Tooltip, Legend } from 'chart.js';
import { toast, esc, fmtTime } from './utils.js';
import { blobBaseUrl, blobHeaders, corsErrorHtml } from './azure.js';

Chart.register(ArcElement, BarElement, BarController, DoughnutController, CategoryScale, LinearScale, Tooltip, Legend);

let _dashboardCases = [];
let _charts = {};

function destroyCharts() {
  for (const c of Object.values(_charts)) { try { c.destroy(); } catch {} }
  _charts = {};
}

export async function loadDashboard() {
  const el = document.getElementById('dashboardContent');
  el.innerHTML = '<p class="empty"><span class="spin"></span> Loading JSONL data for analysis…</p>';
  destroyCharts();

  try {
    const folder = document.getElementById('azOutputFolder').value.trim();
    const prefix = folder + '/jsonl/';
    const url = blobBaseUrl() + `?restype=container&comp=list&prefix=${encodeURIComponent(prefix)}&delimiter=`;
    const resp = await fetch(url, { headers: blobHeaders() });
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    const xml = await resp.text();
    const doc = new DOMParser().parseFromString(xml, 'text/xml');
    const blobs = doc.querySelectorAll('Blob');
    if (!blobs.length) { el.innerHTML = '<p class="empty">No JSONL files found in ' + esc(prefix) + '</p>'; return; }

    const files = [];
    blobs.forEach(b => {
      const name = b.querySelector('Name')?.textContent || '';
      if (name.endsWith('.jsonl')) files.push(name);
    });

    el.innerHTML = `<p class="empty"><span class="spin"></span> Downloading ${files.length} files…</p>`;

    _dashboardCases = [];
    const batchSize = 8;
    for (let i = 0; i < files.length; i += batchSize) {
      const batch = files.slice(i, i + batchSize);
      await Promise.all(batch.map(async name => {
        try {
          const u = blobBaseUrl() + '/' + encodeURIComponent(name).replace(/%2F/g, '/');
          const r = await fetch(u, { headers: blobHeaders() });
          if (!r.ok) return;
          const text = await r.text();
          const first = text.trim().split('\n')[0];
          if (first) _dashboardCases.push(JSON.parse(first));
        } catch {}
      }));
    }

    if (!_dashboardCases.length) { el.innerHTML = '<p class="empty">No valid JSONL data found</p>'; return; }
    renderDashboard();
    toast(`Dashboard loaded: ${_dashboardCases.length} cases`);
  } catch (e) { el.innerHTML = `<p class="empty" style="color:var(--red)">${corsErrorHtml(e.message)}</p>`; }
}

function count(arr) {
  const m = {};
  for (const v of arr) { const k = v || '(empty)'; m[k] = (m[k] || 0) + 1; }
  return m;
}

const PALETTE = ['#0078d4', '#107c10', '#ca5010', '#d13438', '#5c2d91', '#008272', '#b4a0ff', '#ff8c00', '#e3008c', '#00bcf2'];

function renderDashboard() {
  const el = document.getElementById('dashboardContent');
  const cases = _dashboardCases;
  const n = cases.length;

  // Compute stats
  const repos = count(cases.map(c => (c.repo || '').replace('https://github.com/', '')));
  const categories = count(cases.map(c => c.labels?.category || ''));
  const difficulties = count(cases.map(c => c.labels?.difficulty || ''));
  const sources = count(cases.map(c => c.source || ''));
  const mutationTypes = count(cases.map(c => c.mutation_type || ''));
  const localizations = count(cases.map(c => c.labels?.localization || ''));
  const ctxDeps = count(cases.map(c => c.labels?.context_dependency || ''));
  const testMods = count(cases.map(c => c.labels?.test_modality || ''));
  const subTypes = count(cases.map(c => c.labels?.sub_type || ''));
  const dates = count(cases.map(c => (c.created_at || '').substring(0, 10)));

  const linesArr = cases.map(c => c.num_lines_changed || 0);
  const filesArr = cases.map(c => c.num_files_changed || 0);
  const ftpArr = cases.map(c => (c.fail_to_pass || []).length);
  const issueLen = cases.map(c => (c.issue_text || '').length);
  const patchLen = cases.map(c => (c.patches?.gold_patch || '').length);

  const avg = a => a.length ? (a.reduce((s, v) => s + v, 0) / a.length) : 0;

  // Render
  let h = '';

  // KPI cards
  h += `<div class="summary-grid" style="grid-template-columns:repeat(auto-fit,minmax(130px,1fr));margin-bottom:20px">
    <div class="stat-card"><div class="stat-val">${n}</div><div class="stat-label">Total Cases</div></div>
    <div class="stat-card"><div class="stat-val">${Object.keys(repos).length}</div><div class="stat-label">Repositories</div></div>
    <div class="stat-card"><div class="stat-val">${Object.keys(categories).length}</div><div class="stat-label">Categories</div></div>
    <div class="stat-card"><div class="stat-val">${avg(linesArr).toFixed(1)}</div><div class="stat-label">Avg Lines Changed</div></div>
    <div class="stat-card"><div class="stat-val">${avg(ftpArr).toFixed(1)}</div><div class="stat-label">Avg Fail→Pass Tests</div></div>
    <div class="stat-card"><div class="stat-val">${avg(issueLen).toFixed(0)}</div><div class="stat-label">Avg Prompt Length</div></div>
  </div>`;

  // Chart grid
  h += `<div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">`;

  // Chart cards
  const chartCards = [
    { id: 'chartCategory', title: '📊 Category Distribution' },
    { id: 'chartDifficulty', title: '📈 Difficulty Levels' },
    { id: 'chartRepo', title: '📦 Cases per Repository' },
    { id: 'chartMutation', title: '🧬 Mutation Types' },
    { id: 'chartLocalization', title: '🎯 Localization' },
    { id: 'chartContext', title: '🔗 Context Dependency' },
    { id: 'chartTimeline', title: '📅 Cases by Date' },
    { id: 'chartSubType', title: '🏷 Sub Types' },
  ];
  for (const { id, title } of chartCards) {
    h += `<div class="card"><div class="card-head"><span style="font-size:12px">${title}</span></div>
      <div class="card-body" style="padding:12px;height:260px;position:relative"><canvas id="${id}"></canvas></div></div>`;
  }
  h += `</div>`;

  // Detail table: per-repo breakdown
  h += `<div class="card" style="margin-top:16px"><div class="card-head"><span class="icon">📋</span> Per-Repository Breakdown</div>
    <div class="card-body" style="overflow-x:auto"><table class="summary-table">
    <tr><th>Repository</th><th>Cases</th><th>Categories</th><th>Difficulties</th><th>Avg Lines</th><th>Avg Tests</th></tr>`;
  for (const [repo, cnt] of Object.entries(repos).sort((a, b) => b[1] - a[1])) {
    const rc = cases.filter(c => (c.repo || '').replace('https://github.com/', '') === repo);
    const rCats = [...new Set(rc.map(c => c.labels?.category || ''))].join(', ');
    const rDiffs = [...new Set(rc.map(c => c.labels?.difficulty || ''))].sort().join(', ');
    const rLines = avg(rc.map(c => c.num_lines_changed || 0)).toFixed(1);
    const rTests = avg(rc.map(c => (c.fail_to_pass || []).length)).toFixed(1);
    h += `<tr><td style="font-family:monospace;font-size:11px;color:var(--accent)">${esc(repo)}</td>
      <td style="font-weight:600">${cnt}</td><td style="font-size:11px">${esc(rCats)}</td>
      <td style="font-size:11px">${esc(rDiffs)}</td><td>${rLines}</td><td>${rTests}</td></tr>`;
  }
  h += `</table></div></div>`;

  el.innerHTML = h;

  // Draw charts
  setTimeout(() => {
    _charts.category = doughnut('chartCategory', categories);
    _charts.difficulty = doughnut('chartDifficulty', difficulties);
    _charts.repo = bar('chartRepo', repos, 'Cases');
    _charts.mutation = bar('chartMutation', mutationTypes, 'Count');
    _charts.localization = doughnut('chartLocalization', localizations);
    _charts.context = doughnut('chartContext', ctxDeps);
    _charts.timeline = bar('chartTimeline', dates, 'Cases');
    _charts.subType = bar('chartSubType', subTypes, 'Count');
  }, 50);
}

function doughnut(canvasId, dataMap) {
  const canvas = document.getElementById(canvasId);
  if (!canvas) return null;
  const labels = Object.keys(dataMap);
  const values = Object.values(dataMap);
  return new Chart(canvas, {
    type: 'doughnut',
    data: {
      labels,
      datasets: [{ data: values, backgroundColor: PALETTE.slice(0, labels.length), borderWidth: 1, borderColor: '#fff' }],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { position: 'right', labels: { font: { size: 11 }, padding: 8 } },
        tooltip: { bodyFont: { size: 12 } },
      },
    },
  });
}

function bar(canvasId, dataMap, label) {
  const canvas = document.getElementById(canvasId);
  if (!canvas) return null;
  const labels = Object.keys(dataMap);
  const values = Object.values(dataMap);
  return new Chart(canvas, {
    type: 'bar',
    data: {
      labels,
      datasets: [{ label, data: values, backgroundColor: PALETTE[0], borderRadius: 3, maxBarThickness: 40 }],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false }, tooltip: { bodyFont: { size: 12 } } },
      scales: {
        x: { ticks: { font: { size: 10 }, maxRotation: 45 } },
        y: { beginAtZero: true, ticks: { stepSize: 1, font: { size: 10 } } },
      },
    },
  });
}
