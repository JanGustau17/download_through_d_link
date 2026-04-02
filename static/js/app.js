/* ── fastdl · Main Application ────────────────────────────────────────────── */

// ── Session ─────────────────────────────────────────────────────────────────
function getSessionId() {
  let id = document.cookie.match(/(?:^|;\s*)fdl_sid=([^;]*)/)?.[1];
  if (!id) {
    id = crypto.randomUUID ? crypto.randomUUID() : 'x'.replace(/x/g, () =>
      Math.random().toString(36).slice(2, 10)) + Date.now().toString(36);
    document.cookie = `fdl_sid=${id};path=/;max-age=${60*60*24*365};SameSite=Lax`;
  }
  return id;
}
const SESSION_ID = getSessionId();

// ── WebSocket ───────────────────────────────────────────────────────────────
let ws = null;
let wsReady = false;
let wsQueue = [];
let reconnectTimer = null;
const WS_URL = `ws://${location.hostname}:8889`;

function connectWS() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
  ws = new WebSocket(WS_URL);
  ws.onopen = () => { wsReady = true; while (wsQueue.length) ws.send(wsQueue.shift()); };
  ws.onclose = () => { wsReady = false; clearTimeout(reconnectTimer); reconnectTimer = setTimeout(connectWS, 2000); };
  ws.onerror = () => ws.close();
  ws.onmessage = (e) => { try { handleWSMessage(JSON.parse(e.data)); } catch(err) { console.error('WS parse error', err); } };
}

function wsSend(obj) {
  const data = JSON.stringify(obj);
  if (wsReady && ws.readyState === WebSocket.OPEN) ws.send(data);
  else wsQueue.push(data);
}

// ── State ───────────────────────────────────────────────────────────────────
let currentPanel = 'download';
let currentMode = 'video';
let selectedRes = null;
let selectedAudioFormat = 'mp3';
let selectedBitrate = '320';
let probeData = null;
let queue = {};

// YouTube state
let ytAllResults = [];       // All raw results from server
let ytFilteredResults = [];  // After filter/sort
let ytDisplayed = 0;         // How many currently shown
const YT_PAGE_SIZE = 15;
let ytSort = 'relevance';
let ytDurationFilter = 'any';
let ytPopularLoaded = false;
let homePopularLoaded = false;

// Preview state
let previewCache = {};    // video_id -> stream_url
let previewTimer = null;
let previewCurrentId = null;
let previewAbort = null;  // AbortController

// ── Wave Animations ─────────────────────────────────────────────────────────
let heroWave = null;
let previewViz = null;
let spotifyViz = null;

function initWaves() {
  const heroCanvas = document.getElementById('heroWave');
  if (heroCanvas) heroWave = new HeroWave(heroCanvas);
  const prevCanvas = document.getElementById('previewVisualizer');
  if (prevCanvas) previewViz = new AudioVisualizer(prevCanvas);
  const spCanvas = document.getElementById('spotifyVisualizer');
  if (spCanvas) spotifyViz = new AudioVisualizer(spCanvas);
}

// ── Panel Switching ─────────────────────────────────────────────────────────
function switchPanel(name) {
  currentPanel = name;
  document.querySelectorAll('.nav-tab').forEach(btn => btn.classList.toggle('active', btn.dataset.panel === name));
  document.querySelectorAll('.panel').forEach(p => p.classList.toggle('active', p.id === `panel-${name}`));
  closeSidebar();

  // Load popular videos when YouTube tab opened
  if (name === 'youtube' && !ytPopularLoaded) loadYTPopular();

  // Load home popular
  if (name === 'download' && !homePopularLoaded) loadHomePopular();
}

// ── Sidebar (mobile) ────────────────────────────────────────────────────────
function toggleSidebar() { document.getElementById('sidebar').classList.toggle('open'); }
function closeSidebar() { document.getElementById('sidebar').classList.remove('open'); }

// ── Tab Switching (Video / Audio) ───────────────────────────────────────────
function switchTab(mode) {
  currentMode = mode;
  document.getElementById('tabVideo').classList.toggle('active', mode === 'video');
  document.getElementById('tabVideo').classList.toggle('video-active', mode === 'video');
  document.getElementById('tabAudio').classList.toggle('active', mode === 'audio');
  document.getElementById('tabAudio').classList.toggle('audio-active', mode === 'audio');
  document.getElementById('videoPanel').classList.toggle('hide', mode !== 'video');
  document.getElementById('audioPanel').classList.toggle('show', mode === 'audio');
  updateDownloadBtnLabel();
}

// ── Fetch (probe URL) ───────────────────────────────────────────────────────
function handleFetch() {
  const input = document.getElementById('urlInput');
  const url = input.value.trim();
  if (!url) return;
  const btn = document.getElementById('fetchBtn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>Fetching...';
  hideAllCards();
  document.getElementById('inputCard').classList.add('active');
  // Hide popular when fetching
  const hp = document.getElementById('homePopular');
  if (hp) hp.style.display = 'none';
  wsSend({ action: 'probe', url, session_id: SESSION_ID });
}

document.getElementById('urlInput')?.addEventListener('keydown', (e) => { if (e.key === 'Enter') handleFetch(); });

function hideAllCards() {
  ['restrictedCard', 'directCard', 'mediaCard'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.classList.remove('show');
  });
}

// ── Handle Probe Result ─────────────────────────────────────────────────────
function showProbeResult(data) {
  const btn = document.getElementById('fetchBtn');
  btn.disabled = false;
  btn.textContent = 'Fetch';
  hideAllCards();

  if (data.error === 'restricted') {
    document.getElementById('restrictedMsg').textContent = data.message || 'Protected, paywalled, or region-locked.';
    document.getElementById('restrictedCard').classList.add('show');
    return;
  }
  if (data.error) {
    document.getElementById('restrictedMsg').textContent = data.message || 'Could not fetch media info.';
    document.getElementById('restrictedCard').classList.add('show');
    return;
  }
  if (data.type === 'direct') {
    document.getElementById('directFilename').textContent = data.filename || 'File';
    document.getElementById('directCard').classList.add('show');
    probeData = data;
    return;
  }
  probeData = data;
  showMedia(data);
}

function showMedia(data) {
  const card = document.getElementById('mediaCard');
  card.classList.add('show');
  const thumb = document.getElementById('mediaThumbnail');
  if (data.thumbnail) { thumb.src = data.thumbnail; thumb.style.display = ''; } else { thumb.style.display = 'none'; }
  document.getElementById('mediaTitle').textContent = data.title || 'Unknown';
  document.getElementById('mediaUploader').textContent = data.uploader || '';
  document.getElementById('mediaDuration').textContent = data.duration ? formatDuration(data.duration) : '';
  buildResGrid(data.resolutions || []);
  switchTab('video');
  updateDownloadBtnLabel();
  setTimeout(() => card.scrollIntoView({ behavior: 'smooth', block: 'start' }), 100);
}

function buildResGrid(resolutions) {
  const grid = document.getElementById('resGrid');
  grid.innerHTML = '';
  selectedRes = null;
  resolutions.forEach((r, i) => {
    const div = document.createElement('div');
    div.className = 'res-option';
    div.dataset.height = r.height;
    div.dataset.formatId = r.format_id;
    div.onclick = () => selectOption(div);
    div.innerHTML = `<span class="res-label">${r.label}</span><span class="res-size">${r.size || ''}</span>`;
    if (i === resolutions.length - 1) { div.classList.add('selected'); selectedRes = r; }
    grid.appendChild(div);
  });
}

function selectOption(el) {
  document.getElementById('videoPanel').querySelectorAll('.res-option').forEach(o => o.classList.remove('selected'));
  el.classList.add('selected');
  const mode = el.dataset.mode;
  if (mode === 'best') { selectedRes = { height: null, mode: 'best' }; }
  else {
    const height = parseInt(el.dataset.height);
    if (probeData?.resolutions) selectedRes = probeData.resolutions.find(r => r.height === height) || { height };
  }
  updateDownloadBtnLabel();
}

// ── Audio Format & Bitrate ──────────────────────────────────────────────────
function selectAudioFormat(el) {
  document.querySelectorAll('.audio-option').forEach(o => o.classList.remove('selected'));
  el.classList.add('selected');
  selectedAudioFormat = el.dataset.format;
  updateDownloadBtnLabel();
}

function selectBitrate(el) {
  document.querySelectorAll('.bitrate-option').forEach(o => o.classList.remove('selected'));
  el.classList.add('selected');
  selectedBitrate = el.dataset.quality;
  updateDownloadBtnLabel();
}

function getAudioSizeStr() {
  if (!probeData?.duration) return '';
  const dur = probeData.duration, br = parseInt(selectedBitrate) || 320;
  let bytes = 0;
  if (selectedAudioFormat === 'wav') bytes = 44100 * 2 * 2 * dur;
  else if (selectedAudioFormat === 'flac') bytes = 44100 * 2 * 2 * dur * 0.55;
  else bytes = (br * 1000 / 8) * dur;
  return formatSize(bytes);
}

function updateDownloadBtnLabel() {
  const btn = document.getElementById('dlBtn');
  if (!btn) return;
  if (currentMode === 'audio') {
    const s = getAudioSizeStr();
    btn.innerHTML = `&#8595; Download ${selectedAudioFormat.toUpperCase()}${s ? ' (' + s + ')' : ''}`;
  } else if (currentMode === 'video' && selectedRes) {
    if (selectedRes.mode === 'best') {
      const bs = probeData?.best_total_size ? formatSize(probeData.best_total_size) : '';
      btn.innerHTML = `&#8595; Download Best${bs ? ' (' + bs + ')' : ''}`;
    } else {
      btn.innerHTML = `&#8595; Download ${selectedRes.height}p${selectedRes.size ? ' (' + selectedRes.size + ')' : ''}`;
    }
  } else { btn.innerHTML = '&#8595; Download'; }
}

// ── Start Download ──────────────────────────────────────────────────────────
function startDownload() {
  if (!probeData) return;
  const msg = { action: 'download', url: probeData.url, title: probeData.title || '', thumbnail: probeData.thumbnail || '', session_id: SESSION_ID };
  if (currentMode === 'audio') { msg.mode = 'audio'; msg.audio_format = selectedAudioFormat; msg.audio_quality = selectedBitrate; }
  else if (selectedRes?.mode === 'best') { msg.mode = 'best'; }
  else if (selectedRes) { msg.mode = 'video'; msg.height = selectedRes.height; }
  else { msg.mode = 'best'; }
  wsSend(msg);
  document.getElementById('urlInput').value = '';
  hideAllCards();
  const hp = document.getElementById('homePopular');
  if (hp) hp.style.display = '';
  probeData = null;
}

function startDirectDownload() {
  if (!probeData) return;
  wsSend({ action: 'download', url: probeData.url, type: 'direct', title: probeData.filename || '', session_id: SESSION_ID });
  document.getElementById('urlInput').value = '';
  hideAllCards();
  probeData = null;
}

// ── WS Message Handler ──────────────────────────────────────────────────────
function handleWSMessage(msg) {
  if (msg.action === 'probe_result') { showProbeResult(msg); return; }
  if (msg.action === 'download_started') {
    queue[msg.id] = { id: msg.id, title: msg.title || 'Download', thumbnail: msg.thumbnail || '', quality: msg.quality || '', status: 'starting', progress: 0, speed: '', eta: '', total_size: '', downloaded: '' };
    renderQueue();
    return;
  }
  if (msg.id && queue[msg.id]) {
    const item = queue[msg.id];
    if (msg.status) item.status = msg.status;
    if (msg.progress !== undefined) item.progress = msg.progress;
    if (msg.speed !== undefined) item.speed = msg.speed;
    if (msg.eta !== undefined) item.eta = msg.eta;
    if (msg.total_size !== undefined) item.total_size = msg.total_size;
    if (msg.downloaded !== undefined) item.downloaded = msg.downloaded;
    if (msg.filename) item.filename = msg.filename;
    if (msg.path) item.path = msg.path;
    if (msg.size) item.total_size = msg.size;
    if (msg.message) item.message = msg.message;
    renderQueue();
  }
}

// ── Queue Rendering ─────────────────────────────────────────────────────────
function renderQueue() {
  const section = document.getElementById('queueSection');
  const list = document.getElementById('queueList');
  const countEl = document.getElementById('queueCount');
  const ids = Object.keys(queue);
  if (!ids.length) { section.style.display = 'none'; return; }
  section.style.display = '';
  countEl.textContent = ids.length;
  list.innerHTML = ids.map(id => {
    const q = queue[id]; const pct = q.progress || 0;
    let sc = '', si = '&#8987;', st = 'Starting...', ei = '';
    if (q.status === 'downloading') { sc = 'q-downloading'; si = '&#8595;'; st = `${pct.toFixed(1)}%`; const p = []; if (q.speed) p.push(q.speed); if (q.eta && q.eta !== 'Unknown') p.push(`ETA ${q.eta}`); if (q.total_size) p.push(q.total_size); ei = p.join(' &middot; '); }
    else if (q.status === 'processing') { sc = 'q-processing'; si = '&#9881;'; st = 'Processing...'; }
    else if (q.status === 'done') { sc = 'q-done'; si = '&#10003;'; st = 'Done'; ei = q.filename || ''; }
    else if (q.status === 'error') { sc = 'q-error'; si = '&#10007;'; st = 'Failed'; ei = q.message || ''; }
    return `<div class="queue-item ${sc}"><div class="qi-thumb-wrap">${q.thumbnail ? `<img class="qi-thumb" src="${escapeHtml(q.thumbnail)}" alt="" />` : '<div class="qi-thumb-placeholder">&#127916;</div>'}</div><div class="qi-info"><div class="qi-title">${escapeHtml(q.title)}</div><div class="qi-meta"><span class="qi-quality">${escapeHtml(q.quality)}</span><span class="qi-status">${si} ${st}</span>${ei ? `<span class="qi-extra">${ei}</span>` : ''}</div><div class="qi-progress-track"><div class="qi-progress-bar" style="width:${pct}%"></div></div></div>${q.status === 'done' || q.status === 'error' ? `<button class="qi-dismiss" onclick="dismissQueueItem('${id}')">&times;</button>` : ''}</div>`;
  }).join('');
}
function dismissQueueItem(id) { delete queue[id]; renderQueue(); }

// ═══════════════════════════════════════════════════════════════════════════
// ── YOUTUBE SEARCH + LANDING + FILTERS + PREVIEW ─────────────────────────
// ═══════════════════════════════════════════════════════════════════════════

let ytSearching = false;

function searchYouTube() {
  const input = document.getElementById('ytSearchInput');
  const query = input.value.trim();
  if (!query || ytSearching) return;

  ytSearching = true;
  const btn = document.getElementById('ytSearchBtn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>Searching...';
  document.getElementById('ytResults').innerHTML = '<div class="search-loading">Searching YouTube...</div>';
  document.getElementById('ytPopular').style.display = 'none';
  document.getElementById('ytLoadMore').style.display = 'none';
  document.getElementById('ytFilterBar').style.display = 'none';

  fetch(`/api/search/youtube?q=${encodeURIComponent(query)}&session_id=${SESSION_ID}`)
    .then(r => r.json())
    .then(data => {
      ytAllResults = data.results || [];
      ytSort = 'relevance';
      ytDurationFilter = 'any';
      resetFilterChips();
      applyYTFilters();
      document.getElementById('ytFilterBar').style.display = '';
    })
    .catch(err => {
      console.error('YT search error:', err);
      document.getElementById('ytResults').innerHTML = '<div class="search-empty">Search failed.</div>';
    })
    .finally(() => { ytSearching = false; btn.disabled = false; btn.textContent = 'Search'; });
}

document.getElementById('ytSearchInput')?.addEventListener('keydown', (e) => { if (e.key === 'Enter') searchYouTube(); });

// ── Filter / Sort ───────────────────────────────────────────────────────────
function setYTSort(sort, el) {
  ytSort = sort;
  el.parentElement.querySelectorAll('.filter-chip').forEach(c => c.classList.remove('active'));
  el.classList.add('active');
  applyYTFilters();
}

function setYTDuration(dur, el) {
  ytDurationFilter = dur;
  el.parentElement.querySelectorAll('.filter-chip').forEach(c => c.classList.remove('active'));
  el.classList.add('active');
  applyYTFilters();
}

function resetFilterChips() {
  document.querySelectorAll('#ytFilterBar .filter-group').forEach(g => {
    g.querySelectorAll('.filter-chip').forEach((c, i) => c.classList.toggle('active', i === 0));
  });
}

function applyYTFilters() {
  let results = [...ytAllResults];

  // Duration filter
  if (ytDurationFilter === 'short') results = results.filter(r => r.duration && r.duration < 240);
  else if (ytDurationFilter === 'medium') results = results.filter(r => r.duration && r.duration >= 240 && r.duration <= 1200);
  else if (ytDurationFilter === 'long') results = results.filter(r => r.duration && r.duration > 1200);

  // Sort
  if (ytSort === 'views') results.sort((a, b) => (b.view_count || 0) - (a.view_count || 0));
  else if (ytSort === 'longest') results.sort((a, b) => (b.duration || 0) - (a.duration || 0));
  else if (ytSort === 'shortest') results.sort((a, b) => (a.duration || 0) - (b.duration || 0));

  ytFilteredResults = results;
  ytDisplayed = 0;
  document.getElementById('ytResults').innerHTML = '';
  showMoreYTResults();
}

function showMoreYTResults() {
  const container = document.getElementById('ytResults');
  const nextBatch = ytFilteredResults.slice(ytDisplayed, ytDisplayed + YT_PAGE_SIZE);

  if (!ytFilteredResults.length && ytDisplayed === 0) {
    container.innerHTML = '<div class="search-empty">No results match your filters</div>';
    document.getElementById('ytLoadMore').style.display = 'none';
    return;
  }

  nextBatch.forEach(r => {
    container.insertAdjacentHTML('beforeend', renderYTItem(r));
  });

  ytDisplayed += nextBatch.length;

  // Show/hide load more
  const loadMore = document.getElementById('ytLoadMore');
  loadMore.style.display = ytDisplayed < ytFilteredResults.length ? '' : 'none';
}

function loadMoreYT() { showMoreYTResults(); }

function renderYTItem(r) {
  const vid = r.video_id || extractVideoId(r.url) || '';
  return `
    <div class="search-item" data-vid="${vid}"
         onmouseenter="startPreview(this,'${vid}')"
         onmouseleave="stopPreview(this)"
         onclick="loadFromSearch('${escapeAttr(r.url)}')">
      <div class="si-thumb-wrap">
        <img class="si-thumb" src="${escapeHtml(r.thumbnail || '')}" alt="" loading="lazy" />
        ${r.duration ? `<span class="si-duration">${formatDuration(r.duration)}</span>` : ''}
        <div class="si-hover-play">&#9654;</div>
      </div>
      <div class="si-info">
        <div class="si-title">${escapeHtml(r.title || '')}</div>
        <div class="si-meta">
          ${r.uploader ? `<span class="si-uploader">${escapeHtml(r.uploader)}</span>` : ''}
          ${r.view_count ? `<span class="si-views">${formatNumber(r.view_count)} views</span>` : ''}
        </div>
      </div>
      <button class="btn-icon btn-dl-search" onclick="event.stopPropagation(); loadFromSearch('${escapeAttr(r.url)}')" title="Download">&#8595;</button>
    </div>
  `;
}

// ── YouTube Popular / Trending ──────────────────────────────────────────────
function loadYTPopular() {
  ytPopularLoaded = true;
  fetch('/api/popular/youtube')
    .then(r => r.json())
    .then(data => {
      const results = data.results || [];
      const grid = document.getElementById('ytPopularGrid');
      const loading = document.getElementById('ytPopularLoading');
      if (loading) loading.style.display = 'none';
      if (!results.length) { grid.innerHTML = '<div class="search-empty">Could not load trending</div>'; return; }
      grid.innerHTML = results.map(r => renderPopularCard(r)).join('');
    })
    .catch(() => {
      const loading = document.getElementById('ytPopularLoading');
      if (loading) loading.textContent = 'Could not load trending videos';
    });
}

function loadHomePopular() {
  homePopularLoaded = true;
  fetch('/api/popular/youtube')
    .then(r => r.json())
    .then(data => {
      const results = (data.results || []).slice(0, 8);
      const grid = document.getElementById('homePopularGrid');
      if (!grid || !results.length) return;
      grid.innerHTML = results.map(r => renderPopularCard(r)).join('');
    })
    .catch(() => {});
}

function renderPopularCard(r) {
  const vid = r.video_id || extractVideoId(r.url) || '';
  return `
    <div class="popular-card" data-vid="${vid}"
         onmouseenter="startPreview(this,'${vid}')"
         onmouseleave="stopPreview(this)"
         onclick="loadFromSearch('${escapeAttr(r.url)}')">
      <div class="pc-thumb-wrap">
        <img class="pc-thumb" src="${escapeHtml(r.thumbnail || '')}" alt="" loading="lazy" />
        ${r.duration ? `<span class="si-duration">${formatDuration(r.duration)}</span>` : ''}
        <div class="si-hover-play">&#9654;</div>
      </div>
      <div class="pc-info">
        <div class="pc-title">${escapeHtml(r.title || '')}</div>
        <div class="pc-meta">${escapeHtml(r.uploader || '')}${r.view_count ? ` &middot; ${formatNumber(r.view_count)} views` : ''}</div>
      </div>
    </div>
  `;
}

// ═══════════════════════════════════════════════════════════════════════════
// ── HOVER VIDEO PREVIEW (AD-FREE) ───────────────────────────────────────
// ═══════════════════════════════════════════════════════════════════════════

function startPreview(el, videoId) {
  if (!videoId) return;
  clearTimeout(previewTimer);

  previewTimer = setTimeout(() => {
    previewCurrentId = videoId;

    // Position the preview player over the thumbnail
    const thumbWrap = el.querySelector('.si-thumb-wrap, .pc-thumb-wrap');
    if (!thumbWrap) return;

    const rect = thumbWrap.getBoundingClientRect();
    const player = document.getElementById('previewPlayer');
    const video = document.getElementById('previewVideo');
    const loading = document.getElementById('previewLoading');

    // Position
    player.style.left = rect.left + 'px';
    player.style.top = rect.top + 'px';
    player.style.width = rect.width + 'px';
    player.style.height = rect.height + 'px';
    player.style.display = '';
    loading.style.display = '';
    video.style.display = 'none';

    // Check cache
    if (previewCache[videoId]) {
      playPreviewUrl(previewCache[videoId]);
      return;
    }

    // Fetch preview URL
    previewAbort = new AbortController();
    fetch(`/api/preview/youtube?v=${videoId}`, { signal: previewAbort.signal })
      .then(r => r.json())
      .then(data => {
        if (data.url && previewCurrentId === videoId) {
          previewCache[videoId] = data.url;
          playPreviewUrl(data.url);
        } else {
          hidePreviewPlayer();
        }
      })
      .catch(() => hidePreviewPlayer());
  }, 600);
}

function playPreviewUrl(url) {
  const video = document.getElementById('previewVideo');
  const loading = document.getElementById('previewLoading');

  video.src = url;
  video.muted = false;
  video.volume = 0.5;
  video.style.display = '';
  loading.style.display = 'none';

  video.play().catch(() => {
    // Autoplay with sound blocked, try muted
    video.muted = true;
    video.play().catch(() => hidePreviewPlayer());
  });
}

function stopPreview(el) {
  clearTimeout(previewTimer);
  previewCurrentId = null;
  if (previewAbort) { previewAbort.abort(); previewAbort = null; }
  hidePreviewPlayer();
}

function hidePreviewPlayer() {
  const player = document.getElementById('previewPlayer');
  const video = document.getElementById('previewVideo');
  player.style.display = 'none';
  video.pause();
  video.removeAttribute('src');
  video.load();
}

// ═══════════════════════════════════════════════════════════════════════════
// ── SPOTIFY SEARCH ──────────────────────────────────────────────────────
// ═══════════════════════════════════════════════════════════════════════════

let spSearching = false;
let currentPreviewUrl = null;

function searchSpotify() {
  const input = document.getElementById('spSearchInput');
  const query = input.value.trim();
  if (!query || spSearching) return;
  spSearching = true;
  const btn = document.getElementById('spSearchBtn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>Searching...';
  document.getElementById('spResults').innerHTML = '<div class="search-loading">Searching Spotify...</div>';

  fetch(`/api/search/spotify?q=${encodeURIComponent(query)}&session_id=${SESSION_ID}`)
    .then(r => r.json())
    .then(data => renderSpotifyResults(data.results || []))
    .catch(() => { document.getElementById('spResults').innerHTML = '<div class="search-empty">Search failed. Set SPOTIFY_CLIENT_ID & SPOTIFY_CLIENT_SECRET env vars.</div>'; })
    .finally(() => { spSearching = false; btn.disabled = false; btn.textContent = 'Search'; });
}

document.getElementById('spSearchInput')?.addEventListener('keydown', (e) => { if (e.key === 'Enter') searchSpotify(); });

function renderSpotifyResults(results) {
  const container = document.getElementById('spResults');
  if (!results.length) { container.innerHTML = '<div class="search-empty">No results found.</div>'; return; }
  container.innerHTML = results.map(r => `
    <div class="search-item spotify-item">
      <div class="si-thumb-wrap"><img class="si-thumb" src="${escapeHtml(r.thumbnail || '')}" alt="" loading="lazy" /></div>
      <div class="si-info">
        <div class="si-title">${escapeHtml(r.title || '')}</div>
        <div class="si-meta">${r.artist ? `<span class="si-uploader">${escapeHtml(r.artist)}</span>` : ''}${r.album ? `<span class="si-album">${escapeHtml(r.album)}</span>` : ''}</div>
      </div>
      <div class="si-actions">
        ${r.preview_url ? `<button class="btn-icon btn-preview" onclick="event.stopPropagation(); toggleSpotifyPreview('${escapeAttr(r.preview_url)}')" title="Preview">&#9654;</button>` : ''}
        <button class="btn-icon btn-dl-search" onclick="event.stopPropagation(); downloadSpotifyTrack('${escapeAttr(r.title)} ${escapeAttr(r.artist)}')" title="Download">&#8595;</button>
      </div>
    </div>
  `).join('');
}

function toggleSpotifyPreview(url) {
  const audio = document.getElementById('spotifyPreview');
  if (currentPreviewUrl === url && !audio.paused) { audio.pause(); currentPreviewUrl = null; if (spotifyViz) spotifyViz.stop(); return; }
  audio.src = url;
  audio.play().then(() => {
    currentPreviewUrl = url;
    if (spotifyViz) { try { spotifyViz.connectAudio(audio); spotifyViz.start(); } catch(e) { spotifyViz.start(); } }
  }).catch(e => console.warn('Preview blocked:', e));
}

function downloadSpotifyTrack(q) { switchPanel('download'); document.getElementById('urlInput').value = `ytsearch1:${q}`; handleFetch(); }

// ── Load from Search Result ─────────────────────────────────────────────────
function loadFromSearch(url) { switchPanel('download'); document.getElementById('urlInput').value = url; handleFetch(); }

// ── Utility ─────────────────────────────────────────────────────────────────
function formatDuration(s) {
  if (!s) return '';
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = Math.floor(s % 60);
  return h > 0 ? `${h}:${String(m).padStart(2,'0')}:${String(sec).padStart(2,'0')}` : `${m}:${String(sec).padStart(2,'0')}`;
}
function formatSize(b) { if (!b || b <= 0) return ''; const u = ['B','KB','MB','GB','TB']; let i = 0, v = b; while (v >= 1024 && i < u.length-1) { v /= 1024; i++; } return `${v.toFixed(1)} ${u[i]}`; }
function formatNumber(n) { if (!n) return '0'; if (n >= 1e9) return (n/1e9).toFixed(1)+'B'; if (n >= 1e6) return (n/1e6).toFixed(1)+'M'; if (n >= 1e3) return (n/1e3).toFixed(1)+'K'; return n.toString(); }
function escapeHtml(s) { if (!s) return ''; const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }
function escapeAttr(s) { if (!s) return ''; return s.replace(/\\/g,'\\\\').replace(/'/g,"\\'").replace(/"/g,'&quot;'); }
function extractVideoId(url) { if (!url) return ''; const m = url.match(/(?:v=|youtu\.be\/)([^&?#]+)/); return m ? m[1] : ''; }

// ── Init ────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  connectWS();
  initWaves();
  loadHomePopular(); // Load popular videos on home page

  document.addEventListener('click', (e) => {
    const sidebar = document.getElementById('sidebar');
    const toggle = document.querySelector('.mobile-toggle');
    if (sidebar.classList.contains('open') && !sidebar.contains(e.target) && !toggle?.contains(e.target)) closeSidebar();
  });
});

// Global exports
window.switchPanel = switchPanel;
window.toggleSidebar = toggleSidebar;
window.switchTab = switchTab;
window.handleFetch = handleFetch;
window.selectOption = selectOption;
window.selectAudioFormat = selectAudioFormat;
window.selectBitrate = selectBitrate;
window.startDownload = startDownload;
window.startDirectDownload = startDirectDownload;
window.searchYouTube = searchYouTube;
window.searchSpotify = searchSpotify;
window.toggleSpotifyPreview = toggleSpotifyPreview;
window.downloadSpotifyTrack = downloadSpotifyTrack;
window.loadFromSearch = loadFromSearch;
window.dismissQueueItem = dismissQueueItem;
window.setYTSort = setYTSort;
window.setYTDuration = setYTDuration;
window.loadMoreYT = loadMoreYT;
window.startPreview = startPreview;
window.stopPreview = stopPreview;
