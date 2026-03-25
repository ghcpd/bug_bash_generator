import './styles/main.css';

import { STEPS } from './constants.js';
import { toast, esc, toggleCollapse, copyText, clipboardWrite, goStep } from './utils.js';
import { initAuth, importTokens, refreshTokensViaScript, signOut } from './auth.js';
import { setCorsOnStorage } from './azure.js';
import { initSelects, bulkAdd, addRow, updateCount } from './tasks.js';
import { generate, cpRepoList, resetAll, triggerPipeline } from './generate.js';
import { refreshMonitor, renderCombinedRuns, openBatchForRun, loadActivities, viewActivityOutput, cancelRun, backfillRun, doBackfill, toggleAutoRefresh } from './monitor.js';
import { listBatchJobs, renderBatchTasks, loadBatchTasks, viewBatchFile, refreshModal, searchBatchExplorer } from './batch.js';
import { refreshBlobs, previewBlob, switchPreviewTab, switchResultsTab } from './results.js';
import { loadQualityEvaluation, applyQualityFilters, resetQualityFilters } from './quality.js';
import { loadAuditCases, toggleAuditCase, selectAllAudit, selectNoneAudit, runQuickAudit, triggerAuditPipeline, loadAuditResults, previewAuditResult } from './audit.js';
import { loadBugBashCases, toggleBBCase, viewBBCase, bbSelectAll, bbSelectNone, bbSelectWithTar, triggerBugBashGenRubric, confirmTriggerGenRubric, getGenRubricPayload, triggerBugBashAutoRun, loadPipelineDefinition, resetBugBash, exportPromptsAsMd } from './bugbash.js';
import { openModal, closeModal, copyModalContent } from './modal.js';
import { loadDashboard } from './dashboard.js';
import { saveState, loadState, autoSave } from './persistence.js';

// Expose functions globally for inline onclick handlers
window._app = {
  toast, esc, toggleCollapse, copyText, clipboardWrite, goStep,
  importTokens, refreshTokensViaScript, signOut,
  setCorsOnStorage,
  bulkAdd, addRow, updateCount,
  generate, cpRepoList, resetAll, triggerPipeline,
  refreshMonitor, renderCombinedRuns, openBatchForRun, loadActivities, viewActivityOutput, cancelRun, backfillRun, doBackfill, toggleAutoRefresh,
  listBatchJobs, renderBatchTasks, loadBatchTasks, viewBatchFile, refreshModal, searchBatchExplorer,
  refreshBlobs, previewBlob, switchPreviewTab, switchResultsTab,
  loadQualityEvaluation, applyQualityFilters, resetQualityFilters,
  loadAuditCases, toggleAuditCase, selectAllAudit, selectNoneAudit, runQuickAudit, triggerAuditPipeline, loadAuditResults, previewAuditResult,
  loadBugBashCases, toggleBBCase, viewBBCase, bbSelectAll, bbSelectNone, bbSelectWithTar, triggerBugBashGenRubric, confirmTriggerGenRubric, getGenRubricPayload, triggerBugBashAutoRun, loadPipelineDefinition, resetBugBash, exportPromptsAsMd,
  openModal, closeModal, copyModalContent,
  loadDashboard,
  saveState,
};

// Keyboard shortcut for modal
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });

// Initialize MSAL auth
initAuth().catch(e => console.error('MSAL init failed:', e));

// Initialize UI
initSelects();
const restored = loadState();
if (!restored) addRow();
updateCount();
autoSave();

// Restore active tab
try {
  const tab = localStorage.getItem('swebench_active_tab');
  if (tab && STEPS.includes(tab)) {
    goStep(tab);
    if (tab === 'monitor') setTimeout(refreshMonitor, 300);
    if (tab === 'results') setTimeout(refreshBlobs, 300);
    if (tab === 'audit') setTimeout(loadAuditCases, 300);
    if (tab === 'dashboard') setTimeout(loadDashboard, 300);
  }
} catch {}
