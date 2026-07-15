/* ════════════════════════════════════════════════════════════
   KhmerDub — Frontend Logic
   ════════════════════════════════════════════════════════════ */

// ── Detect environment ───────────────────────────────────────
// true when running inside pywebview native window
const IS_NATIVE = typeof window.pywebview !== 'undefined';

// ── DOM refs ─────────────────────────────────────────────────
const dropZone        = document.getElementById('drop-zone');
const fileInput       = document.getElementById('file-input');
const fileInfo        = document.getElementById('file-info');
const fileNameLabel   = document.getElementById('file-name-label');
const fileSizeLabel   = document.getElementById('file-size-label');
const fileRemoveBtn   = document.getElementById('file-remove');
const uploadBtn       = document.getElementById('upload-btn');

const progressPanel   = document.getElementById('progress-panel');
const progressBar     = document.getElementById('progress-bar');
const progressPct     = document.getElementById('progress-pct');
const statusMsg       = document.getElementById('status-msg');
const statusText      = document.getElementById('status-text');
const genderBadgeWrap = document.getElementById('gender-badge-wrap');

const resultSection     = document.getElementById('result-section');
const resultMeta        = document.getElementById('result-meta');
const btnDownloadVideo  = document.getElementById('btn-download-video');
const btnDownloadSrt    = document.getElementById('btn-download-srt');
const btnReset          = document.getElementById('btn-reset');

const origLangTag       = document.getElementById('orig-lang-tag');
const originalTextBox   = document.getElementById('original-text-box');
const translatedTextBox = document.getElementById('translated-text-box');
const segTbody          = document.getElementById('seg-tbody');

// ── State ────────────────────────────────────────────────────
let selectedFile = null;
let currentJobId = null;
let sseSource    = null;

// Stage order for progress UI
const STAGE_ORDER = ['extract','gender','transcribe','translate','tts','merge','complete'];
const stageEls    = {};
STAGE_ORDER.forEach(s => {
  stageEls[s] = document.getElementById(`stage-${s}`);
});

// ── Utilities ────────────────────────────────────────────────
function fmtBytes(b) {
  if (b < 1024)       return b + ' B';
  if (b < 1048576)    return (b/1024).toFixed(1) + ' KB';
  return (b/1048576).toFixed(1) + ' MB';
}

function fmtSecs(s) {
  const h = Math.floor(s/3600), m = Math.floor((s%3600)/60), sec = Math.floor(s%60);
  return [h,m,sec].map(v=>String(v).padStart(2,'0')).join(':');
}

function setStageState(stageName, state /* 'idle'|'active'|'done'|'error' */) {
  const el = stageEls[stageName];
  if (!el) return;
  el.className = 'stage-item';
  const iconEl = el.querySelector('.stage-icon');

  const icons = {
    extract:   '🎵', gender: '🔍', transcribe: '📝',
    translate: '🌏', tts:    '🔊', merge:      '🎬',
    complete:  '✅'
  };

  if (state === 'active') {
    el.classList.add('active');
    iconEl.className = 'stage-icon spin';
    iconEl.textContent = '⚙️';
  } else if (state === 'done') {
    el.classList.add('done');
    iconEl.className = 'stage-icon';
    iconEl.textContent = '✅';
  } else if (state === 'error') {
    el.classList.add('error-stage');
    iconEl.className = 'stage-icon';
    iconEl.textContent = '❌';
  } else {
    iconEl.className = 'stage-icon';
    iconEl.textContent = icons[stageName] || '•';
  }
}

function resetAllStages() {
  STAGE_ORDER.forEach(s => setStageState(s, 'idle'));
}

function updateProgress(data) {
  // Progress bar
  const pct = data.progress || 0;
  progressBar.style.width = pct + '%';
  progressPct.textContent = pct + '%';

  // Stage states
  const activeIdx = STAGE_ORDER.indexOf(data.stage);
  STAGE_ORDER.forEach((s, i) => {
    if (i < activeIdx)       setStageState(s, 'done');
    else if (i === activeIdx) setStageState(s, data.status === 'error' ? 'error' : 'active');
    else                      setStageState(s, 'idle');
  });

  if (data.stage === 'complete') {
    STAGE_ORDER.forEach(s => setStageState(s, 'done'));
  }

  // Status message
  statusText.textContent = data.message || '…';
  statusMsg.className = 'status-msg' + (data.status === 'error' ? ' error-msg' : '');

  // Gender badge
  if (data.gender && !genderBadgeWrap.hasChildNodes()) {
    const badge = document.createElement('div');
    badge.className = `gender-badge ${data.gender}`;
    if (data.gender === 'male') {
      badge.innerHTML = `🎙️ <strong>Male Voice</strong> detected &nbsp;→&nbsp; using <strong>Piseth</strong>`;
    } else {
      badge.innerHTML = `🎙️ <strong>Female Voice</strong> detected &nbsp;→&nbsp; using <strong>Sreymom</strong>`;
    }
    genderBadgeWrap.appendChild(badge);
  }
}

// ── File selection ───────────────────────────────────────────
function selectFile(file) {
  if (!file || !file.type.startsWith('video/')) {
    alert('Please select a valid video file.');
    return;
  }
  selectedFile = file;
  fileNameLabel.textContent = file.name;
  fileSizeLabel.textContent = fmtBytes(file.size);
  fileInfo.style.display = 'flex';
  uploadBtn.disabled = false;
}

function clearFile() {
  selectedFile = null;
  fileInput.value = '';
  fileInfo.style.display = 'none';
  uploadBtn.disabled = true;
}

dropZone.addEventListener('click', () => fileInput.click());

fileInput.addEventListener('change', () => {
  if (fileInput.files[0]) selectFile(fileInput.files[0]);
});

fileRemoveBtn.addEventListener('click', e => { e.stopPropagation(); clearFile(); });

dropZone.addEventListener('dragover', e => {
  e.preventDefault();
  dropZone.classList.add('drag-over');
});
dropZone.addEventListener('dragleave', () => dropZone.classList.remove('drag-over'));
dropZone.addEventListener('drop', e => {
  e.preventDefault();
  dropZone.classList.remove('drag-over');
  const f = e.dataTransfer.files[0];
  if (f) selectFile(f);
});

// ── Upload & process ─────────────────────────────────────────
uploadBtn.addEventListener('click', async () => {
  if (!selectedFile) return;

  // UI: hide upload, show progress
  uploadBtn.disabled = true;
  progressPanel.classList.add('visible');
  resultSection.classList.remove('visible');
  genderBadgeWrap.innerHTML = '';
  resetAllStages();
  progressBar.style.width = '5%';
  progressPct.textContent = '5%';
  statusText.textContent = 'Uploading video…';

  // Upload
  const form  = new FormData();
  form.append('video', selectedFile);
  form.append('mirror', document.getElementById('opt-mirror')?.checked ? 'true' : 'false');
  form.append('blur', document.getElementById('opt-blur')?.checked ? 'true' : 'false');

  let jobId;
  try {
    const res  = await fetch('/upload', { method:'POST', body:form });
    const json = await res.json();
    if (!res.ok) throw new Error(json.error || 'Upload failed');
    jobId = json.job_id;
    currentJobId = jobId;
  } catch (err) {
    statusText.textContent = '❌ Upload failed: ' + err.message;
    statusMsg.className = 'status-msg error-msg';
    uploadBtn.disabled = false;
    return;
  }

  // SSE progress stream
  if (sseSource) sseSource.close();
  sseSource = new EventSource(`/status/${jobId}`);

  sseSource.onmessage = e => {
    let data;
    try { data = JSON.parse(e.data); } catch { return; }

    updateProgress(data);

    if (data.status === 'complete') {
      sseSource.close();
      showResult(jobId, data);
    } else if (data.status === 'error') {
      sseSource.close();
      uploadBtn.disabled = false;
    }
  };

  sseSource.onerror = () => {
    sseSource.close();
    statusText.textContent = '❌ Connection lost. Please try again.';
    statusMsg.className = 'status-msg error-msg';
    uploadBtn.disabled = false;
  };
});

// ── Show results ─────────────────────────────────────────────
async function showResult(jobId, data) {
  resultSection.classList.add('visible');

  // Meta
  const voiceInfo = data.voice || 'Khmer Voice';
  const langInfo  = data.detected_language ? data.detected_language.toUpperCase() : '?';
  resultMeta.textContent =
    `Detected: ${langInfo} → Khmer · ${voiceInfo}`;

  // Orig lang tag
  origLangTag.textContent = langInfo;

  // Texts
  originalTextBox.textContent   = data.original_text   || '—';
  translatedTextBox.textContent = data.translated_text || '—';

  // Downloads
  btnDownloadVideo.onclick = () => downloadFile(data.output_video, 'video');
  btnDownloadSrt.onclick   = () => downloadFile(data.output_srt,   'srt');

  // Open folder button — uses Flask HTTP route (always works)
  const btnFolder = document.getElementById('btn-open-folder');
  if (btnFolder) {
    btnFolder.style.display = 'flex';
    btnFolder.onclick = async () => {
      try {
        const res  = await fetch('/open-output-folder');
        const json = await res.json();
        if (json.ok) showToast('📁 Output folder opened in Explorer');
        else showToast('❌ ' + json.error, true);
      } catch (e) {
        showToast('❌ Could not open folder', true);
      }
    };
  }

  // Fetch segments for subtitle tab
  try {
    const res = await fetch(`/job/${jobId}/segments`);
    const json = await res.json();
    renderSegments(json.segments || []);
  } catch { /* silent */ }
}

// ── Download handler ──────────────────────────────────────────
// Uses Flask HTTP routes — works in BOTH native window AND browser
async function downloadFile(filename, type) {
  if (!filename) return;

  const btn  = type === 'video' ? btnDownloadVideo : btnDownloadSrt;
  const orig = btn.innerHTML;
  btn.innerHTML = '⏳ Saving…';
  btn.disabled  = true;

  try {
    // Call Flask route that copies file to Desktop
    const res  = await fetch(`/save-to-desktop/${filename}`);
    const json = await res.json();

    if (json.ok) {
      showToast(`✅ Saved to Desktop:\n${json.path}`);
    } else {
      // Flask save failed — fallback to blob download
      await blobDownload(filename);
    }
  } catch (e) {
    // Network error — fallback to blob download
    await blobDownload(filename);
  } finally {
    btn.innerHTML = orig;
    btn.disabled  = false;
  }
}

// Blob download fallback (browser mode)
async function blobDownload(filename) {
  try {
    const res  = await fetch(`/download/${filename}`);
    const blob = await res.blob();
    const url  = URL.createObjectURL(blob);
    const a    = document.createElement('a');
    a.href     = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    setTimeout(() => { URL.revokeObjectURL(url); a.remove(); }, 1000);
  } catch (e) {
    showToast('❌ Download failed: ' + e, true);
  }
}

// ── Toast notification ────────────────────────────────────────
function showToast(msg, isError = false) {
  let toast = document.getElementById('kd-toast');
  if (!toast) {
    toast = document.createElement('div');
    toast.id = 'kd-toast';
    toast.style.cssText = `
      position:fixed;bottom:32px;left:50%;transform:translateX(-50%) translateY(20px);
      background:${isError ? 'rgba(239,68,68,.95)' : 'rgba(16,185,129,.95)'};
      color:#fff;padding:14px 28px;border-radius:12px;font-size:14px;font-weight:600;
      box-shadow:0 8px 32px rgba(0,0,0,.4);z-index:9999;
      opacity:0;transition:opacity .3s,transform .3s;pointer-events:none;
      white-space:nowrap;max-width:90vw;text-align:center;
    `;
    document.body.appendChild(toast);
  }
  toast.style.background = isError ? 'rgba(239,68,68,.95)' : 'rgba(16,185,129,.95)';
  toast.textContent = msg;
  toast.style.opacity = '1';
  toast.style.transform = 'translateX(-50%) translateY(0)';
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => {
    toast.style.opacity = '0';
    toast.style.transform = 'translateX(-50%) translateY(20px)';
  }, 4000);
}

function renderSegments(segments) {
  segTbody.innerHTML = '';
  segments.forEach((seg, i) => {
    const tr  = document.createElement('tr');
    const g   = seg.gender || '';
    const icon = g === 'male'   ? '<span title="Piseth (Male)"   style="color:#67e8f9">👨 Piseth</span>'
               : g === 'female' ? '<span title="Sreymom (Female)" style="color:#f9a8d4">👩 Sreymom</span>'
               : '—';
    tr.innerHTML = `
      <td style="color:var(--text-3);width:32px">${i+1}</td>
      <td class="time-cell">${fmtSecs(seg.start)} → ${fmtSecs(seg.end)}</td>
      <td style="width:110px;font-size:12px">${icon}</td>
      <td class="km-cell">${escHtml(seg.text)}</td>
    `;
    segTbody.appendChild(tr);
  });
  if (!segments.length) {
    segTbody.innerHTML = '<tr><td colspan="4" style="color:var(--text-3);padding:20px;text-align:center">No segments available</td></tr>';
  }
}

function escHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

// ── Tabs ─────────────────────────────────────────────────────
document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById(`tab-${btn.dataset.tab}`).classList.add('active');
  });
});

// ── Reset ────────────────────────────────────────────────────
btnReset.addEventListener('click', () => {
  if (sseSource) sseSource.close();
  currentJobId = null;
  clearFile();
  progressPanel.classList.remove('visible');
  resultSection.classList.remove('visible');
  genderBadgeWrap.innerHTML = '';
  resetAllStages();
  progressBar.style.width = '0%';
  progressPct.textContent = '0%';
  statusText.textContent = 'Initialising…';
  statusMsg.className = 'status-msg';
});
