import { addRow } from './tasks.js';

const STORAGE_KEY = 'swebench_prompt_gen';

export function saveState() {
  try {
    const settingIds = ['azSub', 'azRg', 'azFactory', 'azPipeline', 'azStorage', 'azContainer', 'azOutputFolder', 'azModel', 'azTimeout', 'azBatchEndpoint', 'batchPoolFilter', 'auditPipelineName', 'auditOutputFolder', 'auditLevel', 'bbGenRubricPipeline', 'bbAutoRunPipeline', 'bbRubricOutputFolder', 'bbBugBashOutputFolder', 'bbPromptsFolder'];
    const settings = {};
    for (const id of settingIds) { const el = document.getElementById(id); if (el) settings[id] = el.value; }
    const tasks = [];
    for (const tr of document.getElementById('taskBody').children) {
      const repo = tr.querySelector('input[type=text]').value;
      const cat = tr.querySelectorAll('select')[0]?.value || '';
      const diff = tr.querySelectorAll('select')[1]?.value || '';
      const num = tr.querySelector('input[type=number]')?.value || '3';
      tasks.push({ repo, cat, diff, num });
    }
    const sub = document.getElementById('blobSubfolder')?.value || 'jsonl';
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ settings, tasks, blobSubfolder: sub, ts: Date.now() }));
  } catch {}
}

export function loadState() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY); if (!raw) return false;
    const state = JSON.parse(raw);
    if (state.settings) { for (const [id, val] of Object.entries(state.settings)) { if (id === 'azGhToken') continue; const el = document.getElementById(id); if (el && val) el.value = val; } }
    if (state.tasks?.length) { document.getElementById('taskBody').innerHTML = ''; for (const t of state.tasks) addRow(t.repo || '', t.cat || '', t.diff || '', parseInt(t.num) || 3); }
    if (state.blobSubfolder) { const sel = document.getElementById('blobSubfolder'); if (sel) sel.value = state.blobSubfolder; }
    return true;
  } catch { return false; }
}

export function autoSave() {
  document.addEventListener('input', () => { clearTimeout(autoSave._t); autoSave._t = setTimeout(saveState, 500); });
  document.addEventListener('change', () => { clearTimeout(autoSave._t); autoSave._t = setTimeout(saveState, 300); });
  const observer = new MutationObserver(() => { clearTimeout(autoSave._t); autoSave._t = setTimeout(saveState, 500); });
  observer.observe(document.getElementById('taskBody'), { childList: true });
}
