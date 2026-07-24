function app() {
  return {
    tab: 'files',
    localFiles: [],
    libraryItems: [],
    _transcribeQueue: [],
    _transcribeRunning: false,
    currentFile: '',
    segments: [],
    currentSegIdx: 0,
    currentRepeat: 1,
    playing: false,
    repeatMode: false,
    autoScroll: true,
    repeatCount: 3,
    currentTime: 0,
    duration: 0,
    transcribeStatus: '',
    transcribeError: '',
    transcribePoll: null,
    hideText: false,
    splitByPunctuation: false,
    uploading: false,
    transcribingFiles: {},
    settings: {},
    settingsSaved: false,
    wdItems: [],
    wdSort: 'none',  // 'none' | 'asc' | 'desc'
    wdPath: '/',
    wdPathStack: [],
    wdSelectedNas: '',
    wdError: '',
    wdTestMsg: '',
    wdTestOk: false,
    wdTesting: false,
    wdDownloading: false,
    wdDownloadingName: '',
    nasTestingIdx: -1,
    nasTestMsg: {},
    nasTestOk: {},
    editingNasIdx: 0,
    _currentNasIdx: null,
    _currentNasPath: '',
    _audio: null,
    _repeatTimer: null,
    _trackHandler: null,
    _seekToken: 0,
    playMode: 'single',

    // ── 归档 ──────────────────────────────────────────────
    archiveFolders: [],
    archiveItems: [],
    showArchiveModal: false,
    archiveTarget: null,
    archiveFolderInput: '',
    archiveSelectedFolder: '',
    showArchiveSection: false,
    // browse state
    archivePath: '',          // current path inside data/archive/
    archivePathStack: [],     // navigation stack
    archiveBrowseSubdirs: [], // subdirs at current level
    archiveBrowseFiles: [],   // files at current level
    archiveSort: 'none',      // 'none' | 'asc' | 'desc'

    async init() {
      this._audio = document.getElementById('audio-player');
      this._audio.addEventListener('timeupdate', () => {
        this.currentTime = this._audio.currentTime;
      });
      this._audio.addEventListener('loadedmetadata', () => {
        this.duration = this._audio.duration;
      });
      this._audio.addEventListener('ended', () => {
        this.playing = false;
        this._clearTrack();
        this._onTrackEnded();
      });
      await this.loadSettings();
      await this.loadLibrary();
      await this.loadArchiveFolders();
      await this.loadArchiveBrowse();

      // System media controls (lock screen / notification bar)
      if ('mediaSession' in navigator) {
        navigator.mediaSession.setActionHandler('play',         () => this.togglePlay());
        navigator.mediaSession.setActionHandler('pause',        () => this.togglePlay());
        navigator.mediaSession.setActionHandler('previoustrack',() => this.prevSegment());
        navigator.mediaSession.setActionHandler('nexttrack',    () => this.nextSegment());
      }
    },

    // ── 设置 ──────────────────────────────────────────────
    async loadSettings() {
      const r = await fetch('/api/settings');
      this.settings = await r.json();
      this.repeatCount = this.settings.repeat_count ?? 3;
      if (this.settings.nas_list?.length > 0 && this.wdSelectedNas === '') {
        this.wdSelectedNas = 0;
      }
    },

    async saveSettings() {
      await fetch('/api/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(this.settings),
      });
      this.settingsSaved = true;
      setTimeout(() => this.settingsSaved = false, 2000);
    },

    // ── 库 ────────────────────────────────────────────────
    async loadLibrary() {
      const r = await fetch('/api/library');
      const all = await r.json();
      this.libraryItems = all.filter(f => !f.archived);
      this.archiveItems  = all.filter(f =>  f.archived);
      this.localFiles = this.libraryItems.filter(f => f.type === 'local');
    },

    async loadArchiveFolders() {
      const r = await fetch('/api/archive/folders');
      this.archiveFolders = await r.json();
    },

    async loadArchiveBrowse() {
      const r = await fetch(`/api/archive/browse?path=${encodeURIComponent(this.archivePath)}`);
      const data = await r.json();
      this.archiveBrowseSubdirs = data.subdirs;
      this.archiveBrowseFiles   = data.files;
      this._applyArchiveSort();
    },

    _applyArchiveSort() {
      const cmp = (a, b) => a.name.toLowerCase() < b.name.toLowerCase() ? -1 : 1;
      if (this.archiveSort === 'asc') {
        this.archiveBrowseSubdirs = [...this.archiveBrowseSubdirs].sort();
        this.archiveBrowseFiles   = [...this.archiveBrowseFiles].sort(cmp);
      } else if (this.archiveSort === 'desc') {
        this.archiveBrowseSubdirs = [...this.archiveBrowseSubdirs].sort().reverse();
        this.archiveBrowseFiles   = [...this.archiveBrowseFiles].sort(cmp).reverse();
      }
    },

    archiveEnterDir(name) {
      this.archivePathStack.push(this.archivePath);
      this.archivePath = this.archivePath ? this.archivePath + '/' + name : name;
      this.loadArchiveBrowse();
    },

    archiveGoBack() {
      if (!this.archivePathStack.length) return;
      this.archivePath = this.archivePathStack.pop();
      this.loadArchiveBrowse();
    },

    openArchiveModal(item) {
      this.archiveTarget = item;
      this.archiveFolderInput = '';
      this.archiveSelectedFolder = this.archiveFolders[0] || '';
      this.showArchiveModal = true;
    },

    async confirmArchive() {
      const folder = (this.archiveFolderInput.trim() || this.archiveSelectedFolder).trim();
      if (!folder) { alert('请输入或选择归档文件夹'); return; }
      const item = this.archiveTarget;
      const body = item.type === 'nas'
        ? { folder, path: item.path, nas_idx: item.nas_idx }
        : { folder, name: item.name };
      const r = await fetch('/api/archive/archive', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!r.ok) { alert('归档失败: ' + await r.text()); return; }
      this.showArchiveModal = false;
      await this.loadArchiveFolders();
      await this.loadLibrary();
      await this.loadArchiveBrowse();
    },

    async restoreItem(item) {
      const body = item.type === 'nas'
        ? { path: item.path, nas_idx: item.nas_idx }
        : { name: item.name };
      const r = await fetch('/api/archive/restore', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!r.ok) { alert('恢复失败: ' + await r.text()); return; }
      await this.loadLibrary();
      await this.loadArchiveBrowse();
    },

    exportSegments() {
      if (!this.segments.length) return;
      fetch('/api/archive/export', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ filename: this.currentFile, segments: this.segments }),
      }).then(r => {
        const cd = r.headers.get('content-disposition') || '';
        const m = cd.match(/filename="(.+?)"/);
        const name = m ? m[1] : 'export.txt';
        return r.blob().then(b => ({ b, name }));
      }).then(({ b, name }) => {
        const url = URL.createObjectURL(b);
        const a = document.createElement('a');
        a.href = url; a.download = name; a.click();
        setTimeout(() => URL.revokeObjectURL(url), 5000);
      });
    },

    async uploadFile(event) {
      const file = event.target.files[0];
      if (!file) return;
      this.uploading = true;
      const fd = new FormData();
      fd.append('file', file);
      await fetch('/api/files/upload', { method: 'POST', body: fd });
      this.uploading = false;
      await this.loadLibrary();
    },

    _enqueueTranscribe(task) {
      const key = task.type === 'local' ? task.filename : task.path;
      if (this.transcribingFiles[key] || this._transcribeQueue.some(t => (t.filename || t.path) === key)) return;
      this._transcribeQueue.push(task);
      this.transcribingFiles = { ...this.transcribingFiles, [key]: true };
      this._runQueue();
    },

    async _runQueue() {
      if (this._transcribeRunning || this._transcribeQueue.length === 0) return;
      this._transcribeRunning = true;
      const task = this._transcribeQueue.shift();
      try {
        if (task.type === 'local') {
          await this._doTranscribeLocal(task.filename);
        } else {
          await this._doTranscribeNas(task.nasIdx, task.path);
        }
      } finally {
        this._transcribeRunning = false;
        this._runQueue();
      }
    },

    async startTranscribe(filename) {
      console.log('startTranscribe', filename);
      this._enqueueTranscribe({ type: 'local', filename });
    },

    async _doTranscribeLocal(filename) {
      await fetch(`/api/files/transcribe/${encodeURIComponent(filename)}`, { method: 'POST' });
      await new Promise(resolve => {
        const poll = setInterval(async () => {
          const r = await fetch(`/api/files/transcribe/${encodeURIComponent(filename)}/status`);
          const data = await r.json();
          if (data.status === 'done' || data.status === 'error') {
            clearInterval(poll);
            this.transcribingFiles = { ...this.transcribingFiles, [filename]: false };
            await this.loadLibrary();
            resolve();
          }
        }, 2000);
      });
    },

    async startNasTranscribe(nasIdx, remotePath) {
      console.log('startNasTranscribe', nasIdx, remotePath);
      if (!this.libraryItems.some(f => f.type === 'nas' && f.path === remotePath)) {
        await this.addNasToLibrary(nasIdx, remotePath);
      }
      this._enqueueTranscribe({ type: 'nas', nasIdx, path: remotePath });
    },

    async _doTranscribeNas(nasIdx, remotePath) {
      try {
        const resp = await fetch('/api/webdav/nas/transcribe', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ nas_idx: nasIdx, path: remotePath }),
        });
        if (!resp.ok) throw new Error(await resp.text());
        const data = await resp.json();
        if (data.status === 'cached' || data.status === 'done') {
          this.transcribingFiles = { ...this.transcribingFiles, [remotePath]: false };
          await this.loadLibrary();
          return;
        }
        await new Promise(resolve => {
          const poll = setInterval(async () => {
            const r = await fetch('/api/webdav/nas/transcribe/status', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ nas_idx: nasIdx, path: remotePath }),
            });
            const d = await r.json();
            if (d.status === 'done' || d.status === 'error') {
              clearInterval(poll);
              this.transcribingFiles = { ...this.transcribingFiles, [remotePath]: false };
              await this.loadLibrary();
              resolve();
            }
          }, 2000);
        });
      } catch (e) {
        this.transcribingFiles = { ...this.transcribingFiles, [remotePath]: false };
        alert('分句失败: ' + e.message);
      }
    },

    async transcribeAll() {
      const pending = this.libraryItems.filter(f => !f.transcribed && !this.transcribingFiles[f.type === 'local' ? f.name : f.path]);
      for (const f of pending) {
        if (f.type === 'local') {
          this._enqueueTranscribe({ type: 'local', filename: f.name });
        } else {
          this._enqueueTranscribe({ type: 'nas', nasIdx: f.nas_idx, path: f.path });
        }
      }
    },

    async openFile(filename, splitByPunctuation = false) {
      this.tab = 'player';
      this.currentFile = filename;
      this.segments = [];
      this.currentSegIdx = 0;
      this.currentRepeat = 1;
      this._currentNasIdx = null;
      this._currentNasPath = '';
      this._stopAll();

      this._audio.src = `/api/files/audio/${encodeURIComponent(filename)}`;
      this._audio.load();

      // Only load existing cache — do NOT trigger transcription automatically
      const r = await fetch(`/api/files/transcribe/${encodeURIComponent(filename)}/status`);
      const data = await r.json();
      if (data.status === 'done') {
        this.segments = data.segments;
        this.transcribeStatus = 'done';
      } else if (data.status === 'error') {
        this.transcribeStatus = 'error';
        this.transcribeError = data.message;
      } else {
        this.transcribeStatus = '';
      }
    },

    pollTranscribe(filename) {
      this.transcribePoll = setInterval(async () => {
        const r = await fetch(`/api/files/transcribe/${encodeURIComponent(filename)}/status`);
        const data = await r.json();
        if (data.status === 'done') {
          clearInterval(this.transcribePoll);
          this.segments = data.segments;
          this.transcribeStatus = 'done';
          await this.loadLibrary();
        } else if (data.status === 'error') {
          clearInterval(this.transcribePoll);
          this.transcribeStatus = 'error';
          this.transcribeError = data.message;
        }
      }, 2000);
    },

    async deleteFile(filename) {
      if (!confirm(`删除 ${filename}？`)) return;
      await fetch(`/api/files/delete/${encodeURIComponent(filename)}`, { method: 'DELETE' });
      if (this.currentFile === filename) {
        this.currentFile = '';
        this.segments = [];
        this._stopAll();
      }
      await this.loadLibrary();
    },

    async removeLibraryEntry(item) {
      const label = item.name;
      if (!confirm(`从库中移除 ${label}？`)) return;
      await fetch('/api/library/entry', {
        method: 'DELETE',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(item.type === 'nas' ? { path: item.path } : { name: item.name }),
      });
      await this.loadLibrary();
    },

    async selectFile(filename) {
      if (!filename) return;
      await this.openFile(filename);
    },

    // ── NAS 直接播放（不下载到本地）────────────────────────
    async openNasFile(nasIdx, remotePath) {
      this.tab = 'player';
      const streamUrl = `/api/webdav/stream?nas_idx=${nasIdx}&path=${encodeURIComponent(remotePath)}`;
      const displayName = remotePath.split('/').pop();

      this.currentFile = displayName;
      this.segments = [];
      this.currentSegIdx = 0;
      this.currentRepeat = 1;
      this._currentNasIdx = nasIdx;
      this._currentNasPath = remotePath;
      this._stopAll();

      this._audio.src = streamUrl;
      this._audio.load();

      // Register in library
      const nasName = (this.settings.nas_list[nasIdx] || {}).name || '';
      await fetch('/api/library/nas', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ nas_idx: nasIdx, nas_name: nasName, path: remotePath, name: displayName }),
      });
      await this.loadLibrary();

      // Only load existing cache — do NOT trigger transcription automatically
      const r = await fetch('/api/webdav/nas/transcribe/status', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ nas_idx: nasIdx, path: remotePath }),
      });
      const data = await r.json();
      if (data.status === 'done') {
        this.segments = data.segments;
        this.transcribeStatus = 'done';
        await this.loadLibrary();
      } else if (data.status === 'error') {
        this.transcribeStatus = 'error';
        this.transcribeError = data.message;
      } else {
        this.transcribeStatus = '';
      }
    },

    _pollNasTranscribe(nasIdx, remotePath) {
      this.transcribePoll = setInterval(async () => {
        const r = await fetch('/api/webdav/nas/transcribe/status', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ nas_idx: nasIdx, path: remotePath }),
        });
        const data = await r.json();
        if (data.status === 'done') {
          clearInterval(this.transcribePoll);
          this.segments = data.segments;
          this.transcribeStatus = 'done';
          await this.loadLibrary();
        } else if (data.status === 'error') {
          clearInterval(this.transcribePoll);
          this.transcribeStatus = 'error';
          this.transcribeError = data.message;
        }
      }, 2000);
    },

    async reTranscribe() {
      if (!this.currentFile) return;
      this.segments = [];
      this.transcribeStatus = 'processing';
      this.transcribeError = '';
      this._stopAll();

      if (this._currentNasIdx !== null) {
        await fetch('/api/webdav/nas/transcribe/clear', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ nas_idx: this._currentNasIdx, path: this._currentNasPath }),
        });
        const r = await fetch('/api/webdav/nas/transcribe', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ nas_idx: this._currentNasIdx, path: this._currentNasPath, split_by_punctuation: this.splitByPunctuation }),
        });
        const data = await r.json();
        if (data.status === 'done' || data.status === 'cached') {
          this.segments = data.segments;
          this.transcribeStatus = 'done';
        } else {
          this._pollNasTranscribe(this._currentNasIdx, this._currentNasPath);
        }
      } else {
        await fetch(`/api/files/transcribe/${encodeURIComponent(this.currentFile)}/clear`, { method: 'DELETE' });
        await this.openFile(this.currentFile, this.splitByPunctuation);
      }
    },

    // ── 核心 seek ─────────────────────────────────────────
    _seekAndPlay(targetTime, afterSeek) {
      const token = ++this._seekToken;
      this._clearTrack();
      if (this._repeatTimer) { clearTimeout(this._repeatTimer); this._repeatTimer = null; }

      this._audio.pause();

      const onSeeked = () => {
        this._audio.removeEventListener('seeked', onSeeked);
        if (token !== this._seekToken) return;
        afterSeek();
      };
      this._audio.addEventListener('seeked', onSeeked);
      this._audio.currentTime = targetTime;
    },

    // ── 复读模式 ──────────────────────────────────────────
    toggleRepeatMode() {
      this.repeatMode = !this.repeatMode;
      if (this._repeatTimer) { clearTimeout(this._repeatTimer); this._repeatTimer = null; }
      if (this.playing && this.segments.length > 0) {
        this._clearTrack();
        // If audio was paused mid-repeat-gap, resume it
        if (this._audio.paused) this._audio.play();
        this._attachTracker();
      }
    },

    // ── 播放控制 ──────────────────────────────────────────
    get currentSegment() {
      return this.segments[this.currentSegIdx] || null;
    },

    togglePlay() {
      if (this.playing) {
        this._pause();
      } else if (this.segments.length > 0) {
        // 直接从当前位置继续播，不 seek
        this.playing = true;
        this._audio.play();
        this._attachTracker();
      } else {
        this.playing = true;
        this._audio.play();
      }
    },

    _playFromSeg(idx, repeat, targetTime) {
      this._clearTrack();
      this.currentSegIdx = idx;
      this.currentRepeat = repeat;
      this.playing = true;

      this._seekAndPlay(targetTime, () => {
        this._audio.play();
        this._attachTracker();
      });

      if (this.autoScroll) {
        this.$nextTick(() => {
          const el = document.querySelector(`[data-seg-idx="${idx}"]`);
          if (el) el.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
        });
      }
    },

    _attachTracker() {
      this._clearTrack();
      const token = this._seekToken;
      if (this.repeatMode) {
        const seg = this.segments[this.currentSegIdx];
        if (!seg) return;

        // Stop 30ms before seg.end to absorb execution delay.
        // seg.end already has 150ms padding, so last word is still fully audible.
        const stopAt = seg.end - 0.03;
        let fired = false;
        let started = false;
        const stop = () => {
          if (fired || token !== this._seekToken) return;
          fired = true;
          this._clearTrack();
          this._audio.pause();
          this._scheduleRepeatNext(this._audio.currentTime);
        };

        const delay = Math.max(0, (stopAt - this._audio.currentTime) * 1000);
        const timerId = setTimeout(stop, delay);

        const onTimeUpdate = () => {
          const t = this._audio.currentTime;
          if (t >= seg.start && t < stopAt + 1.0) started = true;
          if (started && t >= stopAt) stop();
        };
        this._audio.addEventListener('timeupdate', onTimeUpdate);

        this._trackHandler = { timerId, onTimeUpdate };
      } else {
        this._trackHandler = () => this._trackNormal();
        this._audio.addEventListener('timeupdate', this._trackHandler);
      }
    },

    _clearTrack() {
      if (this._trackHandler === null) return;
      if (typeof this._trackHandler === 'object' && 'timerId' in this._trackHandler) {
        clearTimeout(this._trackHandler.timerId);
        this._audio.removeEventListener('timeupdate', this._trackHandler.onTimeUpdate);
      } else {
        this._audio.removeEventListener('timeupdate', this._trackHandler);
      }
      this._trackHandler = null;
    },

    // 普通模式：只更新高亮，音频不停
    _trackNormal() {
      const t = this._audio.currentTime;
      const idx = this.segments.findIndex(s => t >= s.start && t < s.end);
      if (idx >= 0 && idx !== this.currentSegIdx) {
        this.currentSegIdx = idx;
        if (this.autoScroll) {
          this.$nextTick(() => {
            const el = document.querySelector(`[data-seg-idx="${idx}"]`);
            if (el) el.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
          });
        }
      }
    },

    _scheduleRepeatNext(stoppedAt) {
      const maxRepeat = parseInt(this.repeatCount) || 0;
      const infinite = maxRepeat === 0;

      if (infinite || this.currentRepeat < maxRepeat) {
        const seg = this.segments[this.currentSegIdx];
        this._repeatTimer = setTimeout(() => {
          this._playFromSeg(this.currentSegIdx, this.currentRepeat + 1, seg.start);
        }, this.settings.pause_between_repeats * 1000);
      } else {
        const nextIdx = this.currentSegIdx + 1;
        if (nextIdx < this.segments.length) {
          const nextStart = this.segments[nextIdx].start;
          this._repeatTimer = setTimeout(() => {
            this._playFromSeg(nextIdx, 1, nextStart);
          }, this.settings.pause_between_segments * 1000);
        } else {
          this._repeatTimer = setTimeout(() => {
            this.playing = false;
            this._onTrackEnded();
          }, this.settings.pause_between_segments * 1000);
        }
      }
    },

    _onTrackEnded() {
      if (this.playMode === 'single') return;
      if (this.playMode === 'single-loop') {
        this._audio.currentTime = 0;
        this.currentSegIdx = 0;
        this.currentRepeat = 1;
        this.playing = true;
        this._audio.play();
        if (this.segments.length > 0) this._attachTracker();
        return;
      }
      this._playNextTrack(this.playMode === 'list-loop');
    },

    _playNextTrack(loop) {
      const list = this.libraryItems;
      if (list.length === 0) return;
      const key = this._currentNasIdx !== null ? this._currentNasPath : this.currentFile;
      const idx = list.findIndex(f => f.type === 'nas' ? f.path === key : f.name === key);
      let nextIdx = idx + 1;
      if (nextIdx >= list.length) {
        if (!loop) return;
        nextIdx = 0;
      }
      const next = list[nextIdx];
      if (next.type === 'nas') {
        this.openNasFile(next.nas_idx, next.path);
      } else {
        this.openFile(next.name);
      }
    },

    _pause() {
      this._seekToken++;
      this.playing = false;
      this._audio.volume = 1;
      this._audio.muted = false;
      this._audio.pause();
      this._clearTrack();
      if (this._repeatTimer) { clearTimeout(this._repeatTimer); this._repeatTimer = null; }
    },

    _stopAll() {
      this._pause();
      this.currentRepeat = 1;
      this._seekToken++;
    },

    prevSegment() {
      const idx = Math.max(0, this.currentSegIdx - 1);
      this._playFromSeg(idx, 1, this.segments[idx].start);
    },

    nextSegment() {
      const idx = Math.min(this.segments.length - 1, this.currentSegIdx + 1);
      this._playFromSeg(idx, 1, this.segments[idx].start);
    },

    jumpToSegment(idx) {
      this._playFromSeg(idx, 1, this.segments[idx].start);
    },

    seekFromBar(event) {
      if (!this.duration) return;
      const rect = event.currentTarget.getBoundingClientRect();
      const ratio = (event.clientX - rect.left) / rect.width;
      const targetTime = ratio * this.duration;

      if (this.segments.length > 0) {
        let idx = this.segments.findIndex(s => s.end > targetTime);
        if (idx < 0) idx = this.segments.length - 1;
        this._playFromSeg(idx, 1, targetTime);
      } else {
        this._seekToken++;
        this._audio.currentTime = targetTime;
        if (!this.playing) {
          this.playing = true;
          this._audio.play();
        }
      }
    },

    stopPlayback() {
      this._stopAll();
    },

    // ── WebDAV ────────────────────────────────────────────
    _currentNas() {
      if (this.wdSelectedNas === '') return null;
      return this.settings.nas_list[parseInt(this.wdSelectedNas)] || null;
    },

    async loadWebdav() {
      const nas = this._currentNas();
      if (!nas) return;
      this.wdError = '';
      try {
        const r = await fetch('/api/webdav/list', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ url: nas.url, username: nas.username, password: nas.password, path: this.wdPath }),
        });
        if (!r.ok) { const e = await r.json(); this.wdError = e.detail || '连接失败'; return; }
        const data = await r.json();
        this.wdItems = data.items;
        this._applyWdSort();
      } catch { this.wdError = '网络错误'; }
    },

    _applyWdSort() {
      if (this.wdSort === 'asc') {
        this.wdItems = [...this.wdItems].sort((a, b) => a.name.toLowerCase() < b.name.toLowerCase() ? -1 : 1);
      } else if (this.wdSort === 'desc') {
        this.wdItems = [...this.wdItems].sort((a, b) => a.name.toLowerCase() > b.name.toLowerCase() ? -1 : 1);
      }
    },

    enterDir(path) {
      this.wdPathStack.push(this.wdPath);
      this.wdPath = path;
      this.loadWebdav();
    },

    goBack() {
      if (!this.wdPathStack.length) return;
      this.wdPath = this.wdPathStack.pop();
      this.loadWebdav();
    },

    async downloadWd(item) {
      const nas = this._currentNas();
      if (!nas) return;
      this.wdDownloading = true;
      this.wdDownloadingName = item.name;
      try {
        await fetch('/api/webdav/download', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ url: nas.url, username: nas.username, password: nas.password, path: item.path }),
        });
        await this.loadLibrary();
      } catch { alert('下载失败'); }
      this.wdDownloading = false;
      this.wdDownloadingName = '';
    },

    async downloadAndPlay(item) {
      const nas = this._currentNas();
      if (!nas) return;
      this.wdDownloading = true;
      this.wdDownloadingName = item.name;
      try {
        const r = await fetch('/api/webdav/download', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ url: nas.url, username: nas.username, password: nas.password, path: item.path }),
        });
        const data = await r.json();
        await this.loadLibrary();
        this.wdDownloading = false;
        this.wdDownloadingName = '';
        await this.openFile(data.name);
      } catch {
        alert('下载失败');
        this.wdDownloading = false;
        this.wdDownloadingName = '';
      }
    },

    addNas() {
      if (!this.settings.nas_list) this.settings.nas_list = [];
      this.settings.nas_list.push({ name: '', url: '', username: '', password: '', root: '/' });
      this.editingNasIdx = this.settings.nas_list.length - 1;
    },

    removeNas(i) {
      this.settings.nas_list.splice(i, 1);
      this.editingNasIdx = Math.min(this.editingNasIdx, this.settings.nas_list.length - 1);
    },

    async testNas(i) {
      const nas = this.settings.nas_list[i];
      this.nasTestingIdx = i;
      this.nasTestMsg = { ...this.nasTestMsg, [i]: '' };
      try {
        const r = await fetch('/api/webdav/test', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ url: nas.url, username: nas.username, password: nas.password, root: nas.root }),
        });
        if (r.ok) {
          const d = await r.json();
          this.nasTestOk = { ...this.nasTestOk, [i]: true };
          this.nasTestMsg = { ...this.nasTestMsg, [i]: `连接成功 ✓（${d.items_count} 个文件）` };
        } else {
          const e = await r.json();
          this.nasTestOk = { ...this.nasTestOk, [i]: false };
          this.nasTestMsg = { ...this.nasTestMsg, [i]: e.detail || '连接失败' };
        }
      } catch {
        this.nasTestOk = { ...this.nasTestOk, [i]: false };
        this.nasTestMsg = { ...this.nasTestMsg, [i]: '网络错误' };
      }
      this.nasTestingIdx = -1;
      setTimeout(() => { this.nasTestMsg = { ...this.nasTestMsg, [i]: '' }; }, 5000);
    },

    async addNasToLibrary(nasIdx, remotePath) {
      const nasName = (this.settings.nas_list[nasIdx] || {}).name || '';
      const name = remotePath.split('/').pop();
      await fetch('/api/library/nas', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ nas_idx: nasIdx, nas_name: nasName, path: remotePath, name }),
      });
      await this.loadLibrary();
    },

    nasIsTranscribed(nasIdx, path) {
      const entry = this.libraryItems.find(f => f.type === 'nas' && f.nas_idx === nasIdx && f.path === path);
      return entry ? entry.transcribed : false;
    },

    isAudio(name) {
      return /\.(mp3|wav|m4a|mp4|flac|ogg|aac)$/i.test(name);
    },

    isPdf(name) {
      return /\.pdf$/i.test(name);
    },

    openPdf(nasIdx, path) {
      const url = `/api/webdav/pdf?nas_idx=${nasIdx}&path=${encodeURIComponent(path)}`;
      window.open(url, '_blank');
    },


    // ── 工具函数 ──────────────────────────────────────────
    formatTime(seconds) {
      if (!seconds || isNaN(seconds)) return '0:00';
      const m = Math.floor(seconds / 60);
      const s = Math.floor(seconds % 60).toString().padStart(2, '0');
      return `${m}:${s}`;
    },

    formatBytes(bytes) {
      if (bytes < 1024) return bytes + ' B';
      if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
      return (bytes / 1024 / 1024).toFixed(1) + ' MB';
    },
  };
}
