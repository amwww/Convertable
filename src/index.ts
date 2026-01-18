import type {
  DroppedFile,
  ConversionJob,
  ConversionResultItem,
  EngineEvent,
} from './models.js';

interface ConvertableAPI {
  enqueueJobs(jobs: { srcPath: string; targetExt: string }[]): Promise<void>;
  onEngineEvent(handler: (event: EngineEvent) => void): () => void;
}

declare global {
  interface Window {
    convertable?: ConvertableAPI;
  }
}

function setupTabs() {
  const tabButtons = Array.from(
    document.querySelectorAll<HTMLButtonElement>('[data-tab]'),
  );
  const tabPanels = Array.from(
    document.querySelectorAll<HTMLElement>('.tab-panel'),
  );

  tabButtons.forEach((btn) => {
    btn.addEventListener('click', () => {
      const tab = btn.dataset.tab;
      if (!tab) return;

      tabButtons.forEach((b) => b.classList.toggle('active', b === btn));
      tabPanels.forEach((panel) => {
        panel.classList.toggle('active', panel.id === `tab-${tab}`);
      });
    });
  });
}

function extFromName(name: string): string {
  const idx = name.lastIndexOf('.');
    if (idx <= 0 || idx === name.length - 1) return '—';
  return name.slice(idx).toUpperCase();
}

function humanSize(bytes: number | null): string {
  if (bytes == null) return '';
    if (bytes == null) return '—';
  let size = bytes;
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  for (const unit of units) {
    if (size < 1024 || unit === 'TB') {
      if (unit === 'B') return `${Math.trunc(size)} ${unit}`;
      return `${(size / 1024).toFixed(1)} ${unit}`;
    }
    size /= 1024;
  }
  return `${bytes} B`;
}

function setupConvertTab() {
  const dropZone = document.getElementById('drop-zone');
  const fileList = document.getElementById('file-list');
  const targetSelect = document.getElementById(
    'target-ext-select',
  ) as HTMLSelectElement | null;
  const convertButton = document.getElementById(
    'convert-button',
  ) as HTMLButtonElement | null;

  if (!dropZone || !fileList || !targetSelect || !convertButton) return;

  const dropped: DroppedFile[] = [];
  const queue: ConversionJob[] = [];
  const results: ConversionResultItem[] = [];

  function renderFiles() {
    if (!fileList) return;
    fileList.innerHTML = '';
    for (const f of dropped) {
      const row = document.createElement('div');
      row.className = 'file-row';
      const name = document.createElement('span');
      name.className = 'file-name';
      name.textContent = f.name;
      const size = document.createElement('span');
      size.className = 'file-size';
      size.textContent = humanSize(f.sizeBytes);
      const ext = document.createElement('span');
      ext.className = 'file-ext';
      ext.textContent = f.ext;
      row.append(name, size, ext);
      fileList.appendChild(row);
    }
  }

  function renderQueue() {
    const queueList = document.getElementById('queue-list');
    if (!queueList) return;
    queueList.innerHTML = '';
    for (const job of queue) {
      const row = document.createElement('div');
      row.className = 'queue-row';
      const name = document.createElement('span');
      name.className = 'queue-name';
      name.textContent = job.sourceName;
      const status = document.createElement('span');
      status.className = 'queue-status';
      status.textContent = job.status;
      row.append(name, status);
      queueList.appendChild(row);
    }
  }

  function renderResults() {
    const resultList = document.getElementById('result-list');
    if (!resultList) return;
    resultList.innerHTML = '';
    for (const item of results) {
      const row = document.createElement('div');
      row.className = 'result-row';
      const name = document.createElement('span');
      name.className = 'result-name';
      name.textContent = item.sourceName;
      const target = document.createElement('span');
      target.className = 'result-ext';
      target.textContent = item.targetExt;
      row.append(name, target);
      resultList.appendChild(row);
    }
  }

  function handleEngineEvent(ev: EngineEvent) {
    if (ev.type === 'start') {
      const job = queue.find(
        (j) => j.sourcePath === ev.srcPath && j.targetExt === ev.targetExt,
      );
      if (job) {
        job.status = 'Processing';
        renderQueue();
      }
    } else if (ev.type === 'progress') {
      // Could update a progress bar per job in the future.
    } else if (ev.type === 'done') {
      const job = queue.find(
        (j) => j.sourcePath === ev.srcPath && j.targetExt === ev.targetExt,
      );
      if (job) {
        job.status = 'Done';
        renderQueue();
      }
      results.push({
        sourcePath: ev.srcPath,
        sourceName: job?.sourceName ?? ev.srcPath,
        outputPath: ev.outputPath,
        targetExt: ev.targetExt,
      });
      renderResults();
    } else if (ev.type === 'error') {
      const job = queue.find((j) => j.sourcePath === ev.srcPath);
      if (job) {
        job.status = `Error`;
        renderQueue();
      }
      // In the future, surface the error message to the user.
    }
  }

  dropZone.addEventListener('dragover', (e) => {
    e.preventDefault();
    dropZone.classList.add('drag-over');
  });

  dropZone.addEventListener('dragleave', (e) => {
    e.preventDefault();
    dropZone.classList.remove('drag-over');
  });

  dropZone.addEventListener('drop', (e) => {
    e.preventDefault();
    dropZone.classList.remove('drag-over');
    const dt = e.dataTransfer;
    if (!dt) return;
    const files = Array.from(dt.files);
    for (const file of files) {
      const anyFile = file as any;
      const fullPath: string = typeof anyFile.path === 'string'
        ? anyFile.path
        : file.name;
      const ext = extFromName(file.name);
      const item: DroppedFile = {
        path: fullPath,
        name: file.name,
        sizeBytes: typeof file.size === 'number' ? file.size : null,
        mime: file.type || 'application/octet-stream',
        ext,
      };
      dropped.push(item);
    }
    renderFiles();
  });

  convertButton.addEventListener('click', async () => {
    if (!window.convertable) {
      // Backend not wired yet.
      return;
    }
    const targetExt = targetSelect.value.trim();
    if (!targetExt) return;
    const jobs = dropped.map<ConversionJob>((f) => ({
      sourcePath: f.path,
      sourceName: f.name,
      targetExt,
      status: 'Queued',
    }));
    queue.splice(0, queue.length, ...jobs);
    renderQueue();

    await window.convertable.enqueueJobs(
      jobs.map((j) => ({ srcPath: j.sourcePath, targetExt: j.targetExt })),
    );
  });

  if (window.convertable) {
    window.convertable.onEngineEvent(handleEngineEvent);
  }
}

window.addEventListener('DOMContentLoaded', () => {
  setupTabs();
  setupConvertTab();
});
