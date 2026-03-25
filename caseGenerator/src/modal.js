import { toast, clipboardWrite } from './utils.js';

export function openModal(title, content, showRefresh) {
  document.getElementById('logModalTitle').textContent = title;
  document.getElementById('logModalContent').textContent = content;
  document.getElementById('modalRefreshBtn').style.display = showRefresh ? '' : 'none';
  document.getElementById('logModal').classList.add('show');
  document.body.style.overflow = 'hidden';
}

export function closeModal() {
  document.getElementById('logModal').classList.remove('show');
  document.body.style.overflow = '';
}

export function copyModalContent() {
  const text = document.getElementById('logModalContent').textContent;
  clipboardWrite(text).then(() => toast('Content copied!'));
}
