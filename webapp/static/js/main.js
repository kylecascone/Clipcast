/* ── ClipCast Studio — Main JS ─────────────────────────────────────────────── */

// ── Global state ─────────────────────────────────────────────────────────────
window._pipelineRunning = false;

// ── SocketIO connection ───────────────────────────────────────────────────────
const socket = io({ transports: ['websocket', 'polling'] });

socket.on('connect', () => {
  updatePipelineStatus(false);
});

socket.on('disconnect', () => {
  setPipelineDot('error', 'Disconnected');
});

socket.on('log', (data) => {
  appendLog(data);
});

socket.on('job_status', (data) => {
  const running = data.status === 'started';
  window._pipelineRunning = running;
  updatePipelineStatus(running, data.status);

  if (data.status === 'done') {
    showToast('Pipeline complete!', 'success');
    loadStats();
  } else if (data.status === 'error') {
    showToast('Pipeline encountered an error', 'error');
  } else if (data.status === 'already_running') {
    showToast('Pipeline is already running', 'info');
  }
});

// ── Log feed ──────────────────────────────────────────────────────────────────
const LOG_MAX_LINES = 300;

function appendLog(data) {
  const feed = document.getElementById('log-feed');
  if (!feed) return;

  const line = document.createElement('div');
  line.className = `log-line ${data.level || 'info'}`;
  line.innerHTML = `<span class="log-ts">${data.ts || ''}</span><span class="log-msg">${escHtmlStr(data.msg || '')}</span>`;
  feed.appendChild(line);

  // Trim old lines
  while (feed.children.length > LOG_MAX_LINES) {
    feed.removeChild(feed.firstChild);
  }

  // Auto-scroll to bottom
  feed.scrollTop = feed.scrollHeight;

  // Mirror to editor log if present
  const editorFeed = document.getElementById('editor-log');
  if (editorFeed && editorFeed !== feed) {
    const clone = line.cloneNode(true);
    editorFeed.appendChild(clone);
    while (editorFeed.children.length > LOG_MAX_LINES) {
      editorFeed.removeChild(editorFeed.firstChild);
    }
    editorFeed.scrollTop = editorFeed.scrollHeight;
  }
}

function clearLog() {
  const feed = document.getElementById('log-feed');
  if (feed) feed.innerHTML = '';
}

// ── Pipeline status ───────────────────────────────────────────────────────────
function updatePipelineStatus(running, statusStr) {
  window._pipelineRunning = running;
  const dot   = document.getElementById('pipeline-dot');
  const label = document.getElementById('pipeline-label');

  if (!dot || !label) return;

  if (running) {
    setPipelineDot('running', 'Running...');
  } else if (statusStr === 'error') {
    setPipelineDot('error', 'Error');
  } else {
    setPipelineDot('', 'Idle');
  }

  // Update run buttons
  const btns = document.querySelectorAll('#btn-run-test, #btn-run-live');
  btns.forEach(btn => { btn.disabled = running; });
}

function setPipelineDot(cls, text) {
  const dot   = document.getElementById('pipeline-dot');
  const label = document.getElementById('pipeline-label');
  if (dot)   { dot.className = 'status-dot ' + (cls || ''); }
  if (label) { label.textContent = text || 'Idle'; }
}

// ── Stats ─────────────────────────────────────────────────────────────────────
function loadStats() {
  fetch('/api/stats')
    .then(r => r.json())
    .then(d => {
      if (d.error) return;
      setEl('stat-pool',      d.pool_size);
      setEl('stat-queue',     d.queue_pending);
      setEl('stat-today',     d.posts_today);
      setEl('stat-processed', d.videos_processed);

      // Nav badge
      const navBadge = document.getElementById('nav-queue-count');
      if (navBadge) {
        navBadge.textContent = d.queue_pending > 0 ? d.queue_pending : '';
      }

      if (d.pipeline_running !== undefined) {
        updatePipelineStatus(d.pipeline_running, d.pipeline_running ? 'started' : 'idle');
      }
    })
    .catch(() => {}); // Silently ignore network errors
}

function setEl(id, val) {
  const el = document.getElementById(id);
  if (el && val !== undefined && val !== null) el.textContent = val;
}

// ── Toast notifications ───────────────────────────────────────────────────────
function showToast(msg, type = 'info') {
  let container = document.getElementById('toast-container');
  if (!container) {
    container = document.createElement('div');
    container.id = 'toast-container';
    document.body.appendChild(container);
  }

  const icons = { success: '✓', error: '✗', info: 'ℹ' };
  const toast = document.createElement('div');
  toast.className = `toast-cc ${type}`;
  toast.innerHTML = `<span style="font-weight:700">${icons[type] || ''}</span> ${escHtmlStr(msg)}`;
  container.appendChild(toast);

  setTimeout(() => {
    toast.style.transition = 'opacity 0.3s';
    toast.style.opacity = '0';
    setTimeout(() => toast.remove(), 350);
  }, 3500);
}

// ── Sidebar toggle (mobile) ───────────────────────────────────────────────────
const sidebarToggle = document.getElementById('sidebar-toggle');
const sidebar = document.getElementById('sidebar');

if (sidebarToggle && sidebar) {
  sidebarToggle.addEventListener('click', () => {
    sidebar.classList.toggle('open');
  });

  // Close sidebar when clicking outside on mobile
  document.getElementById('main-content')?.addEventListener('click', () => {
    if (window.innerWidth <= 768) sidebar.classList.remove('open');
  });
}

// ── Utility ───────────────────────────────────────────────────────────────────
function escHtmlStr(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// ── Init ──────────────────────────────────────────────────────────────────────
loadStats();
setInterval(loadStats, 15000);
