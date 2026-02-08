import type {
	DroppedFile,
	ConversionJob,
	ConversionResultItem,
	EngineEvent,
} from './models.js';

interface ConvertableAPI {
	getPathsForFiles(files: File[]): string[];
	pickFiles(): Promise<DroppedFile[]>;
	getFileMetadata(paths: string[]): Promise<DroppedFile[]>;
	revealInFinder(path: string): Promise<void>;
	startDrag(path: string): void;
	copyToClipboard?(text: string): Promise<void>;
	getOutputDir(): Promise<{ configured: string | null; effective: string }>;
	pickOutputDir(): Promise<{ configured: string | null; effective: string; canceled: boolean }>;
	resetOutputDir(): Promise<{ configured: string | null; effective: string }>;
	getCpuThreads?(): Promise<number | null>;
	setCpuThreads?(threads: number | null): Promise<number | null>;
	cancelCurrent?(): Promise<{ canceled: boolean } | void>;
	cancelWorker?(workerId: number): Promise<{ canceled: boolean } | void>;
	cancelAll?(): Promise<{ canceledCurrent: boolean; canceledPending: number } | void>;
	getWorkerCount?(): Promise<number>;
	setWorkerCount?(count: number): Promise<number>;
	getPaused?(): Promise<boolean>;
	setPaused?(paused: boolean): Promise<boolean>;
	setPendingQueues?(queues: { srcPath: string; targetExt: string; workerId?: number }[][]): Promise<void>;
	enqueueJobs(jobs: { srcPath: string; targetExt: string; workerId?: number }[]): Promise<void>;
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

	const setActiveTab = (tab: string) => {
		tabButtons.forEach((b) => b.classList.toggle('active', b.dataset.tab === tab));
		tabPanels.forEach((panel) => {
			panel.classList.toggle('active', panel.id === `tab-${tab}`);
		});
		try {
			localStorage.setItem('convertable:lastTab', tab);
		} catch {
			// ignore
		}
	};
	
	tabButtons.forEach((btn) => {
		btn.addEventListener('click', () => {
			const tab = btn.dataset.tab;
			if (!tab) return;
			setActiveTab(tab);
		});
	});

	// Restore last tab.
	try {
		const last = localStorage.getItem('convertable:lastTab');
		if (last && tabButtons.some((b) => b.dataset.tab === last)) {
			setActiveTab(last);
		}
	} catch {
		// ignore
	}

	return setActiveTab;
}

function extFromName(name: string): string {
	const idx = name.lastIndexOf('.');
	if (idx <= 0 || idx === name.length - 1) return '—';
	return name.slice(idx).toUpperCase();
}

function humanSize(bytes: number | null): string {
	if (bytes == null) return '—';
	const mb = bytes / (1024 * 1024);
	return `${mb.toFixed(2)} MB`;
}

function humanBytes(bytes: number | null): string {
	if (bytes == null) return '—';
	const abs = Math.max(0, bytes);
	const units = ['B', 'KB', 'MB', 'GB', 'TB'];
	let u = 0;
	let v = abs;
	while (v >= 1024 && u < units.length - 1) {
		v /= 1024;
		u += 1;
	}
	const digits = u === 0 ? 0 : u === 1 ? 1 : 2;
	return `${v.toFixed(digits)} ${units[u]}`;
}

function humanDuration(seconds: number | null): string {
	if (seconds == null || !Number.isFinite(seconds) || seconds < 0) return '—';
	const s = Math.round(seconds);
	const h = Math.floor(s / 3600);
	const m = Math.floor((s % 3600) / 60);
	const sec = s % 60;
	if (h > 0) return `${h}h ${m}m`;
	if (m > 0) return `${m}m ${sec}s`;
	return `${sec}s`;
}

function basename(p: string): string {
	const parts = p.split(/[/\\]/g);
	return parts[parts.length - 1] || p;
}

function setupConvertTab() {
	const dropZone = document.getElementById('drop-zone');
	const fileList = document.getElementById('file-list');
	const convertProgress = document.getElementById('convert-progress');
	const convertProgressLabel = document.getElementById('convert-progress-label');
	const convertProgressStatus = document.getElementById('convert-progress-status');
	const convertProgressFill = document.getElementById('convert-progress-fill');
	const pickButton = document.getElementById(
		'pick-files-button',
	) as HTMLButtonElement | null;
	const convertDisabledReason = document.getElementById('convert-disabled-reason');
	const selectionCount = document.getElementById('selection-count');
	const targetSelect = document.getElementById(
		'target-ext-select',
	) as HTMLSelectElement | null;
	const convertButton = document.getElementById(
		'convert-button',
	) as HTMLButtonElement | null;
	const clearFilesButton = document.getElementById('clear-files-button') as HTMLButtonElement | null;
	const clearResultsButton = document.getElementById('clear-results-button') as HTMLButtonElement | null;
	const resultFilter = document.getElementById('result-filter') as HTMLInputElement | null;
	const outputDirLabel = document.getElementById('output-dir-label');
	const outputDirChange = document.getElementById('output-dir-change') as HTMLButtonElement | null;
	const outputDirReset = document.getElementById('output-dir-reset') as HTMLButtonElement | null;
	const outputDirReveal = document.getElementById('output-dir-reveal') as HTMLButtonElement | null;
	const cpuThreadsSelect = document.getElementById('cpu-threads-select') as HTMLSelectElement | null;
	const cancelAllButton = document.getElementById('cancel-all-button') as HTMLButtonElement | null;
	const pauseButton = document.getElementById('pause-button') as HTMLButtonElement | null;
	const workerInc = document.getElementById('worker-inc') as HTMLButtonElement | null;
	const workerDec = document.getElementById('worker-dec') as HTMLButtonElement | null;
	const workerCountLabel = document.getElementById('worker-count');
	const workersContainer = document.getElementById('workers-container');
	const toastContainer = document.getElementById('toast-container');
	const resultContextMenu = document.getElementById('result-context-menu');
	
	if (!dropZone || !fileList || !targetSelect || !convertButton) return;
	
	const dropped: DroppedFile[] = [];

	type WorkerState = {
		workerId: number;
		running: ConversionJob | null;
		pending: ConversionJob[];
	};

	const workers: WorkerState[] = [];
	let workerCount = 1;
	let paused = false;
	let lastActiveWorkerId = 0;
	const results: ConversionResultItem[] = [];
	const selected = new Set<string>();
	let lastSelectedIndex = -1;

	let lastRenderedDroppedCount = 0;
	let lastRenderedResultsCount = 0;

	let runTotal = 0;
	let runDone = 0;
	let currentJobKey: string | null = null;
	let currentJobName: string | null = null;
	let currentJobProgress = 0;
	let runStartMs: number | null = null;
	let runEndMs: number | null = null;
	let runSuccessCount = 0;
	let runErrorCount = 0;
	let runCanceledCount = 0;
	let runCompleteNotified = false;
	let runJobKeys: string[] = [];
	const jobProgressByKey = new Map<string, number>();
	const jobBytesByKey = new Map<string, number>();
	let hideProgressTimer: number | null = null;
	let resultsFilterText = '';
	let outputDirEffective: string | null = null;
	let outputDirConfigured: string | null = null;
	let contextMenuResultIndex: number | null = null;
	let dragPendingFrom: { workerId: number; index: number } | null = null;
	let isDraggingPending = false;
	let dragInteractionGuardUntil = 0;
	let renderWorkersScheduled = false;
	const scheduleRenderWorkers = () => {
		if (renderWorkersScheduled) return;
		renderWorkersScheduled = true;
		window.requestAnimationFrame(() => {
			renderWorkersScheduled = false;
			renderWorkers();
		});
	};

	function toast(message: string, kind: 'info' | 'success' | 'error' = 'info', timeoutMs = 3800) {
		if (!toastContainer) return;
		const el = document.createElement('div');
		el.className = `toast toast--${kind}`;
		const msg = document.createElement('div');
		msg.textContent = message;
		const close = document.createElement('button');
		close.type = 'button';
		close.className = 'toast-close';
		close.textContent = 'Close';
		let timer: number | null = null;
		let removing = false;
		let removeAfterTimer: number | null = null;
		const remove = () => {
			if (removing) return;
			removing = true;
			if (timer != null) {
				window.clearTimeout(timer);
				timer = null;
			}
			// Play exit animation if available.
			el.classList.add('toast--out');
			removeAfterTimer = window.setTimeout(() => {
				removeAfterTimer = null;
				el.remove();
			}, 140);
		};
		close.addEventListener('click', remove);
		el.addEventListener('click', (ev) => {
			if (ev.target === close) return;
			remove();
		});
		el.append(msg, close);
		toastContainer.appendChild(el);
		if (timeoutMs > 0) timer = window.setTimeout(remove, timeoutMs);
	}

	function canSystemNotify(): boolean {
		return typeof window.Notification !== 'undefined' && Notification.permission !== 'denied';
	}

	async function ensureNotificationPermission(): Promise<boolean> {
		if (typeof window.Notification === 'undefined') return false;
		if (Notification.permission === 'granted') return true;
		if (Notification.permission === 'denied') return false;
		try {
			const p = await Notification.requestPermission();
			return p === 'granted';
		} catch {
			return false;
		}
	}

	function shouldNotifyInSystemTray(): boolean {
		// Avoid spamming when the user is actively watching the app.
		try {
			if (document.hidden) return true;
			if (typeof document.hasFocus === 'function') return !document.hasFocus();
		} catch {
			// ignore
		}
		return false;
	}

	async function systemNotify(title: string, body: string) {
		if (!shouldNotifyInSystemTray()) return;
		if (!canSystemNotify()) return;
		const ok = await ensureNotificationPermission();
		if (!ok) return;
		try {
			new Notification(title, { body });
		} catch {
			// ignore
		}
	}

	function maybeNotifyRunComplete() {
		if (runCompleteNotified) return;
		if (runTotal <= 0) return;
		if (runDone < runTotal) return;
		runCompleteNotified = true;

		const parts: string[] = [];
		if (runSuccessCount > 0) parts.push(`${runSuccessCount} done`);
		if (runErrorCount > 0) parts.push(`${runErrorCount} error${runErrorCount === 1 ? '' : 's'}`);
		if (runCanceledCount > 0) parts.push(`${runCanceledCount} canceled`);
		const summary = parts.length > 0 ? parts.join(' • ') : `${runTotal} finished`;
		void systemNotify('Convertable — Finished', summary);
	}

	async function copyText(text: string) {
		const api = window.convertable;
		try {
			if (api?.copyToClipboard) {
				await api.copyToClipboard(text);
				return;
			}
			if (navigator.clipboard?.writeText) {
				await navigator.clipboard.writeText(text);
				return;
			}
		} catch {
			// ignore
		}
		throw new Error('Clipboard copy is not available.');
	}

	function hideContextMenu() {
		if (!resultContextMenu) return;
		resultContextMenu.setAttribute('hidden', '');
		contextMenuResultIndex = null;
	}

	function showContextMenu(clientX: number, clientY: number, resultIndex: number) {
		if (!resultContextMenu) return;
		contextMenuResultIndex = resultIndex;
		resultContextMenu.removeAttribute('hidden');
		// Position and clamp within viewport.
		const pad = 8;
		const rect = resultContextMenu.getBoundingClientRect();
		const maxX = Math.max(pad, window.innerWidth - rect.width - pad);
		const maxY = Math.max(pad, window.innerHeight - rect.height - pad);
		const left = Math.max(pad, Math.min(maxX, clientX));
		const top = Math.max(pad, Math.min(maxY, clientY));
		(resultContextMenu as HTMLElement).style.left = `${left}px`;
		(resultContextMenu as HTMLElement).style.top = `${top}px`;
	}

	function renderOutputDir() {
		if (!outputDirLabel) return;
		const text = outputDirConfigured ? outputDirConfigured : 'Temp (session)';
		outputDirLabel.textContent = text;
		outputDirLabel.title = outputDirEffective ?? '';
		const hasEffective = !!outputDirEffective;
		if (outputDirReveal) outputDirReveal.disabled = !hasEffective;
		if (outputDirReset) outputDirReset.disabled = !outputDirConfigured;
	}

	async function refreshOutputDir() {
		try {
			const api = window.convertable;
			if (!api?.getOutputDir) return;
			const info = await api.getOutputDir();
			outputDirConfigured = info.configured;
			outputDirEffective = info.effective;
			renderOutputDir();
		} catch {
			// ignore
		}
	}

	async function refreshCpuThreads() {
		if (!cpuThreadsSelect) return;
		const api = window.convertable;
		if (!api?.getCpuThreads || !api?.setCpuThreads) {
			cpuThreadsSelect.style.display = 'none';
			return;
		}
		try {
			const v = await api.getCpuThreads();
			cpuThreadsSelect.value = String(v ?? 0);
		} catch {
			// ignore
		}
	}

	cpuThreadsSelect?.addEventListener('change', () => {
		void (async () => {
			try {
				const api = window.convertable;
				if (!api?.setCpuThreads) return;
				const raw = Number(cpuThreadsSelect.value);
				const threads = Number.isFinite(raw) && raw >= 1 ? Math.floor(raw) : null;
				const saved = await api.setCpuThreads(threads);
				cpuThreadsSelect.value = String(saved ?? 0);
				toast(saved ? `CPU limit: ${saved} thread${saved === 1 ? '' : 's'}` : 'CPU limit: Auto', 'info', 2200);
			} catch (err) {
				const msg = err instanceof Error ? err.message : String(err);
				toast(msg || 'Failed to set CPU limit', 'error');
			}
		})();
	});

	type SourceKind = 'image' | 'audio' | 'video' | 'archive' | 'other';

	function kindFromFile(f: DroppedFile): SourceKind {
		// Prefer extension-based detection (more reliable across platforms), then mime.
		const ext = (f.ext || '').toUpperCase();
		if (ext === '.PDF') return 'image';
		if (['.ZIP', '.RAR', '.7Z', '.TAR', '.TAR.GZ', '.TGZ'].includes(ext)) return 'archive';
		const m = (f.mime || '').toLowerCase();
		if (m === 'application/pdf' || m === 'application/x-pdf' || m.endsWith('/pdf')) return 'image';
		if (m.startsWith('image/')) return 'image';
		if (m.startsWith('audio/')) return 'audio';
		if (m.startsWith('video/')) return 'video';
		return 'other';
	}

	function allowedTargetsForKind(kind: SourceKind): string[] {
		if (kind === 'image') return ['.PNG', '.JPEG', '.WEBP', '.PDF'];
		if (kind === 'audio') return ['.MP3', '.WAV', '.M4A'];
		if (kind === 'video') return ['.MP4', '.MOV', '.MP3', '.WAV', '.M4A'];
		if (kind === 'archive') return ['.EXTRACT', '.ZIP', '.TAR', '.TAR.GZ', '.TGZ', '.7Z'];
		return [];
	}

	function activePaths(): string[] {
		return selected.size > 0 ? Array.from(selected) : dropped.map((f) => f.path);
	}

	function updateSelectionCount() {
		if (!selectionCount) return;
		const total = dropped.length;
		const active = activePaths().length;
		if (total === 0) {
			selectionCount.textContent = '0 selected';
			return;
		}
		selectionCount.textContent = `${active} selected`;
	}

	function selectionKind(): SourceKind | null {
		const paths = activePaths();
		if (paths.length === 0) return null;
		const kinds = new Set<SourceKind>();
		for (const p of paths) {
			const f = dropped.find((x) => x.path === p);
			if (!f) continue;
			kinds.add(kindFromFile(f));
		}
		if (kinds.size !== 1) return 'other';
		return kinds.values().next().value ?? 'other';
	}

	function selectionAllPdf(): boolean {
		const paths = activePaths();
		if (paths.length === 0) return false;
		for (const p of paths) {
			const f = dropped.find((x) => x.path === p);
			if (!f) return false;
			if ((f.ext || '').toUpperCase() !== '.PDF') return false;
		}
		return true;
	}

	function updateConvertLayout() {
		const hasFiles = dropped.length > 0;
		if (dropZone) dropZone.style.display = hasFiles ? 'none' : '';
		if (fileList) fileList.style.display = hasFiles ? '' : 'none';
		const footer = document.querySelector<HTMLElement>('#tab-convert .convert-footer');
		if (footer) footer.style.display = hasFiles ? '' : 'none';
		if (convertProgress) convertProgress.hidden = !hasFiles;
	}

	if (outputDirChange) {
		outputDirChange.addEventListener('click', async () => {
			try {
				const api = window.convertable;
				if (!api?.pickOutputDir) return;
				await api.pickOutputDir();
				await refreshOutputDir();
			} catch {
				// ignore
			}
		});
	}

	if (outputDirReset) {
		outputDirReset.addEventListener('click', async () => {
			try {
				const api = window.convertable;
				if (!api?.resetOutputDir) return;
				await api.resetOutputDir();
				await refreshOutputDir();
			} catch {
				// ignore
			}
		});
	}

	if (outputDirReveal) {
		outputDirReveal.addEventListener('click', async () => {
			try {
				const api = window.convertable;
				if (!api?.revealInFinder) return;
				const p = outputDirEffective;
				if (!p) return;
				await api.revealInFinder(p);
			} catch {
				// ignore
			}
		});
	}

	function updateTargetOptionsAndConvertState() {
		if (!targetSelect || !convertButton) return;

		const kind = selectionKind();
		let allowed = kind ? allowedTargetsForKind(kind) : [];
		// Don't offer PDF->PDF.
		if (selectionAllPdf()) {
			allowed = allowed.filter((x) => x !== '.PDF');
		}
		const prev = targetSelect.value;
		targetSelect.innerHTML = '';
		for (const ext of allowed) {
			const opt = document.createElement('option');
			opt.value = ext;
			opt.textContent = ext;
			targetSelect.appendChild(opt);
		}

		// Restore last target per kind when possible.
		let desired: string | null = null;
		if (kind && kind !== 'other') {
			try {
				desired = localStorage.getItem(`convertable:lastTarget:${kind}`);
			} catch {
				// ignore
			}
		}
		if (desired && allowed.includes(desired)) targetSelect.value = desired;
		else if (allowed.includes(prev)) targetSelect.value = prev;
		else if (allowed.length > 0) targetSelect.value = allowed[0] ?? '';

		const hasSelection = activePaths().length > 0;
		const supported = allowed.length > 0;
		const mixedOrUnsupported = kind === 'other';
		convertButton.disabled = !hasSelection || !supported || mixedOrUnsupported;
		targetSelect.disabled = !hasSelection || !supported;

		let reason = '';
		if (!hasSelection) {
			reason = 'Add files to convert.';
		} else if (mixedOrUnsupported) {
			reason = 'Mixed or unsupported file types selected. Select only images, only audio, or only video.';
		} else if (!supported) {
			reason = 'No supported conversions for this selection.';
		}
		convertButton.title = reason;
		if (convertDisabledReason) {
			convertDisabledReason.textContent = reason;
			convertDisabledReason.hidden = !reason;
		}
	}

	targetSelect.addEventListener('change', () => {
		const kind = selectionKind();
		if (!kind || kind === 'other') return;
		try {
			localStorage.setItem(`convertable:lastTarget:${kind}`, targetSelect.value);
		} catch {
			// ignore
		}
	});

	function clearHideProgressTimer() {
		if (hideProgressTimer != null) {
			window.clearTimeout(hideProgressTimer);
			hideProgressTimer = null;
		}
	}

	function updateConvertProgress() {
		if (!convertProgress || !convertProgressLabel || !convertProgressStatus || !convertProgressFill) return;
		convertProgressLabel.textContent = 'Queue';

		const bar = convertProgress.querySelector<HTMLElement>('.convert-progress__bar');
		const running = runTotal > 0 && runDone < runTotal;
		if (!running) {
			convertProgressStatus.textContent = 'No jobs running';
			convertProgressFill.style.width = '0%';
			if (bar) bar.style.display = 'none';
			return;
		}
		if (bar) bar.style.display = '';

		const overall = overallProgress();
		const pct = Math.round(overall * 100);
		const parts: string[] = [];
		parts.push(`${pct}%`);
		parts.push(`${Math.min(runDone, runTotal)}/${runTotal}`);
		const rc = runningCount();
		if (rc > 0) parts.push(`Running: ${rc}`);
		const active = workers[Math.max(0, Math.min(workers.length - 1, lastActiveWorkerId))];
		if (active?.running?.sourceName) parts.push(active.running.sourceName);
		convertProgressStatus.textContent = parts.join(' — ');
		convertProgressFill.style.width = `${pct}%`;
	}

	function renderWorkers() {
		if (!workersContainer) return;
		ensureWorkers(workerCount);
		workersContainer.innerHTML = '';

		for (const w of workers) {
			const card = document.createElement('div');
			card.className = 'worker-card';
			card.dataset.workerId = String(w.workerId);

			const header = document.createElement('div');
			header.className = 'worker-header';

			const title = document.createElement('div');
			title.className = 'worker-title';
			title.textContent = `Worker ${w.workerId + 1}`;

			const meta = document.createElement('div');
			meta.className = 'worker-meta';
			const runningText = w.running && w.running.status === 'Processing'
				? `Running: ${w.running.sourceName}`
				: w.running
					? `${w.running.status}: ${w.running.sourceName}`
					: 'Idle';
			meta.textContent = paused ? `${runningText} (paused)` : runningText;

			const actions = document.createElement('div');
			actions.className = 'worker-actions';
			const cancel = document.createElement('button');
			cancel.type = 'button';
			cancel.className = 'btn btn-secondary';
			cancel.textContent = 'Cancel worker';
			cancel.disabled = !(w.running && w.running.status === 'Processing') && w.pending.length === 0;
			cancel.addEventListener('click', () => {
				void (async () => {
					try {
						await window.convertable?.cancelWorker?.(w.workerId);
						toast(`Cancel requested for worker ${w.workerId + 1}`, 'info');
					} catch (err) {
						const msg = err instanceof Error ? err.message : String(err);
						toast(msg || 'Cancel failed', 'error');
					}
				})();
			});
			actions.appendChild(cancel);

			header.append(title, meta, actions);
			card.appendChild(header);

			const list = document.createElement('div');
			list.className = 'worker-queue';

			const addJobRow = (job: ConversionJob, kind: 'running' | 'pending', pendingIndex: number | null) => {
				const row = document.createElement('div');
				row.className = 'worker-job';
				row.classList.toggle('is-running', kind === 'running');
				row.classList.toggle('is-queued', kind === 'pending');
				row.dataset.kind = kind;
				row.dataset.workerId = String(w.workerId);
				if (pendingIndex != null) row.dataset.index = String(pendingIndex);
				if (kind === 'pending') row.draggable = true;

				const name = document.createElement('div');
				name.className = 'worker-job__name';
				name.textContent = job.sourceName;
				const status = document.createElement('div');
				status.className = 'worker-job__status';
				const p = typeof job.progress === 'number' ? Math.max(0, Math.min(1, job.progress)) : 0;
				const pct = kind === 'running' ? ` ${(p * 100).toFixed(0)}%` : '';
				status.textContent = `${job.status}${pct}`;
				row.append(name, status);

				if (kind === 'pending') {
					// Guard against frequent progress re-renders interrupting drag initiation.
					row.addEventListener('pointerdown', () => {
						dragInteractionGuardUntil = Date.now() + 1500;
					});
					row.addEventListener('pointerup', () => {
						dragInteractionGuardUntil = 0;
					});
					row.addEventListener('pointercancel', () => {
						dragInteractionGuardUntil = 0;
					});

					row.addEventListener('dragstart', (ev) => {
						const idx = pendingIndex ?? -1;
						dragPendingFrom = { workerId: w.workerId, index: idx };
						isDraggingPending = true;
						dragInteractionGuardUntil = 0;
						try {
							ev.dataTransfer?.setData('text/plain', `${w.workerId}:${idx}`);
							ev.dataTransfer?.setData(
								'application/x-convertable-pending',
								JSON.stringify({ workerId: w.workerId, index: idx }),
							);
						} catch {
							// ignore
						}
						if (ev.dataTransfer) ev.dataTransfer.effectAllowed = 'move';
					});
					row.addEventListener('dragend', () => {
						dragPendingFrom = null;
						if (isDraggingPending) {
							isDraggingPending = false;
							scheduleRenderWorkers();
						}
					});
				}

				return row;
			};

			if (w.running) list.appendChild(addJobRow(w.running, 'running', null));
			for (let i = 0; i < w.pending.length; i += 1) {
				const job = w.pending[i];
				if (!job) continue;
				list.appendChild(addJobRow(job, 'pending', i));
			}
			if (!w.running && w.pending.length === 0) {
				const empty = document.createElement('div');
				empty.className = 'empty-state';
				empty.textContent = 'Drop jobs here';
				list.appendChild(empty);
			}

			const canAcceptDrop = () => {
				return !!dragPendingFrom;
			};

			list.addEventListener('dragover', (ev) => {
				if (!canAcceptDrop()) return;
				ev.preventDefault();
				list.classList.add('drag-over');
				if (ev.dataTransfer) ev.dataTransfer.dropEffect = 'move';
			});
			list.addEventListener('dragleave', () => {
				list.classList.remove('drag-over');
			});
			list.addEventListener('drop', (ev) => {
				if (!canAcceptDrop()) return;
				ev.preventDefault();
				ev.stopPropagation();
				list.classList.remove('drag-over');

				const from = dragPendingFrom;
				dragPendingFrom = null;
				isDraggingPending = false;
				if (!from) return;
				if (from.workerId < 0 || from.workerId >= workers.length) return;
				const src = workers[from.workerId];
				const item = src?.pending?.[from.index];
				if (!src || !item) return;

				let insertAt = w.pending.length;
				const targetJob = (ev.target as HTMLElement | null)?.closest('.worker-job[data-kind="pending"]') as HTMLElement | null;
				if (targetJob) {
					const idxStr = targetJob.dataset.index;
					const n = idxStr != null ? Number(idxStr) : NaN;
					if (Number.isFinite(n) && n >= 0) insertAt = Math.max(0, Math.min(w.pending.length, Math.floor(n)));
				}

				// Remove from source.
				src.pending.splice(from.index, 1);
				// If moving within same worker, adjust insert index.
				if (from.workerId === w.workerId && from.index < insertAt) insertAt = Math.max(0, insertAt - 1);
				w.pending.splice(insertAt, 0, item);
				w.pending.forEach((j) => (j.workerId = w.workerId));
				src.pending.forEach((j) => (j.workerId = src.workerId));
				lastActiveWorkerId = w.workerId;

				scheduleRenderWorkers();
				void syncPendingQueuesToEngine();
			});

			card.appendChild(list);
			workersContainer.appendChild(card);
		}

		updateQueueStats();
		updateConvertProgress();
		if (cancelAllButton) {
			const any = workers.some((w) => (w.running && w.running.status === 'Processing') || w.pending.length > 0);
			cancelAllButton.disabled = !any;
		}
	}

	function jobKey(srcPath: string, targetExt: string): string {
		return `${srcPath}::${targetExt}`;
	}

	function ensureWorkers(count: number) {
		let next = Math.floor(count);
		if (!Number.isFinite(next) || next < 1) next = 1;
		if (next > 16) next = 16;
		workerCount = next;
		while (workers.length < workerCount) {
			workers.push({ workerId: workers.length, running: null, pending: [] });
		}
		if (workers.length > workerCount) {
			// Merge pending from removed workers into the last one.
			const last = workerCount - 1;
			for (let w = workerCount; w < workers.length; w += 1) {
				const removed = workers[w];
				if (removed?.pending?.length) workers[last]?.pending.push(...removed.pending);
			}
			workers.length = workerCount;
		}
		workers.forEach((w, idx) => {
			w.workerId = idx;
			w.pending.forEach((j) => (j.workerId = idx));
			if (w.running) w.running.workerId = idx;
		});
		if (workerCountLabel) workerCountLabel.textContent = String(workerCount);
		if (pauseButton) pauseButton.textContent = paused ? 'Resume' : 'Pause';
		if (workerDec) {
			const lastWorkerRunning = workers[workerCount - 1]?.running?.status === 'Processing';
			workerDec.disabled = workerCount <= 1 || lastWorkerRunning;
		}
	}

	function runningCount(): number {
		let c = 0;
		for (const w of workers) if (w.running && w.running.status === 'Processing') c += 1;
		return c;
	}

	function allPendingQueuesForEngine(): { srcPath: string; targetExt: string; workerId?: number }[][] {
		return workers.map((w, workerId) =>
			w.pending.map((j) => ({ srcPath: j.sourcePath, targetExt: j.targetExt, workerId })),
		);
	}

	async function syncPendingQueuesToEngine() {
		const api = window.convertable;
		if (!api?.setPendingQueues) return;
		try {
			await api.setPendingQueues(allPendingQueuesForEngine());
		} catch {
			// ignore
		}
	}

	function chooseWorkerForNewJob(): number {
		let best = 0;
		let bestScore = Number.POSITIVE_INFINITY;
		for (let i = 0; i < workers.length; i += 1) {
			const w = workers[i];
			if (!w) continue;
			const score = w.pending.length + (w.running && w.running.status === 'Processing' ? 1 : 0);
			if (score < bestScore) {
				best = i;
				bestScore = score;
			}
		}
		return best;
	}

	function enqueueToWorker(job: ConversionJob, workerId?: number) {
		ensureWorkers(workerCount);
		const wid = workerId ?? chooseWorkerForNewJob();
		const w = workers[Math.max(0, Math.min(workers.length - 1, wid))];
		if (!w) return;
		job.workerId = w.workerId;
		w.pending.push(job);
		lastActiveWorkerId = w.workerId;

		const k = jobKey(job.sourcePath, job.targetExt);
		const isNewRun = !runStartMs || runTotal <= 0 || runDone >= runTotal;
		if (isNewRun) {
			runStartMs = Date.now();
			runEndMs = null;
			runTotal = 1;
			runDone = 0;
			runSuccessCount = 0;
			runErrorCount = 0;
			runCanceledCount = 0;
			runCompleteNotified = false;
			runJobKeys = [k];
			jobProgressByKey.clear();
			jobBytesByKey.clear();
			jobProgressByKey.set(k, 0);
		} else {
			runTotal += 1;
			runJobKeys.push(k);
			jobProgressByKey.set(k, 0);
		}
		const meta = dropped.find((x) => x.path === job.sourcePath);
		if (meta?.sizeBytes != null) jobBytesByKey.set(k, meta.sizeBytes);

		clearHideProgressTimer();
		updateConvertProgress();
		scheduleRenderWorkers();
	}

	function overallProgress(): number {
		if (runTotal <= 0 || runJobKeys.length === 0) return 0;
		let sum = 0;
		let count = 0;
		for (const k of runJobKeys) {
			const p = jobProgressByKey.get(k);
			if (typeof p !== 'number') continue;
			sum += Math.max(0, Math.min(1, p));
			count += 1;
		}
		if (count <= 0) return 0;
		return Math.max(0, Math.min(1, sum / count));
	}

	function processedBytesAndTotal(): { processed: number | null; total: number | null } {
		let total = 0;
		let processed = 0;
		let haveAny = false;
		for (const k of runJobKeys) {
			const b = jobBytesByKey.get(k);
			if (typeof b !== 'number' || !Number.isFinite(b) || b <= 0) continue;
			haveAny = true;
			total += b;
			const p = jobProgressByKey.get(k) ?? 0;
			processed += b * Math.max(0, Math.min(1, p));
		}
		if (!haveAny) return { processed: null, total: null };
		return { processed, total };
	}

	function updateQueueStats() {
		const elOverall = document.getElementById('stat-overall-pct');
		const elElapsed = document.getElementById('stat-elapsed');
		const elEta = document.getElementById('stat-eta');
		const elCurrent = document.getElementById('stat-current');
		const elBytes = document.getElementById('stat-bytes');
		const elJobs = document.getElementById('stat-jobs');
		const elOverallFill = document.getElementById('stat-overall-fill') as HTMLElement | null;
		if (!elOverall || !elElapsed || !elEta || !elCurrent || !elBytes || !elJobs) return;

		const running = runTotal > 0 && runDone < runTotal;
		const p = overallProgress();
		elOverall.textContent = `${Math.round(p * 100)}%`;
		if (elOverallFill) elOverallFill.style.width = `${(p * 100).toFixed(1)}%`;
		elJobs.textContent = `${Math.min(runDone, runTotal)}/${runTotal}`;

		if (runStartMs == null) {
			elElapsed.textContent = '—';
		} else {
			const endMs = runEndMs ?? Date.now();
			const elapsedSec = Math.max(0, (endMs - runStartMs) / 1000);
			elElapsed.textContent = humanDuration(elapsedSec);
		}

		if (!running) {
			elEta.textContent = '—';
			elCurrent.textContent = 'No jobs running';
		} else {
			const active = workers[Math.max(0, Math.min(workers.length - 1, lastActiveWorkerId))];
			const runningJob = active?.running?.status === 'Processing'
				? active.running
				: workers.find((w) => w.running?.status === 'Processing')?.running;
			if (runningJob) {
				const rp = typeof runningJob.progress === 'number' ? runningJob.progress : 0;
				elCurrent.textContent = `${runningJob.sourceName} (${Math.round(rp * 100)}%)`;
			} else {
				elCurrent.textContent = runningCount() > 0 ? 'Running…' : '—';
			}
			let eta: number | null = null;
			if (runStartMs != null && p > 0.02) {
				const elapsed = (Date.now() - runStartMs) / 1000;
				eta = (elapsed * (1 - p)) / p;
			}
			elEta.textContent = humanDuration(eta);
		}

		const { processed, total } = processedBytesAndTotal();
		elBytes.textContent =
			processed == null || total == null ? '—' : `${humanBytes(processed)} / ${humanBytes(total)}`;
	}

	async function extractPathsFromDataTransfer(dt: DataTransfer | null): Promise<string[]> {
		if (!dt) return [];
		const paths: string[] = [];

		// Preferred: dt.files
		for (const file of Array.from(dt.files)) {
			const anyFile = file as any;
			const fullPath: string = typeof anyFile.path === 'string' ? anyFile.path : '';
			if (!fullPath) continue;
			paths.push(fullPath);
		}
		if (paths.length > 0) return paths;

		// If Electron doesn't expose File.path, use preload webUtils.getPathForFile.
		if (window.convertable && (dt.files?.length ?? 0) > 0) {
			try {
				const resolved = window.convertable.getPathsForFiles(Array.from(dt.files));
				for (const p of resolved) {
					if (typeof p === 'string' && p) paths.push(p);
				}
				if (paths.length > 0) return paths;
			} catch {
				// ignore
			}
		}

		// Fallback: dt.items (sometimes dt.files is empty on macOS/Electron)
		for (const item of Array.from(dt.items ?? [])) {
			if (item.kind !== 'file') continue;
			const f = item.getAsFile();
			if (!f) continue;
			const anyFile = f as any;
			const fullPath: string = typeof anyFile.path === 'string' ? anyFile.path : '';
			if (!fullPath) continue;
			paths.push(fullPath);
		}

		if (paths.length > 0) return paths;

		// Fallback: URI lists (Finder drag sometimes provides file:// URLs)
		const uriPayloads: string[] = [];
		for (const type of ['text/uri-list', 'public.file-url', 'text/plain']) {
			try {
				const v = dt.getData(type);
				if (typeof v === 'string' && v.trim()) uriPayloads.push(v);
			} catch {
				// ignore
			}
		}
		for (const payload of uriPayloads) {
			const lines = payload
				.split(/\r?\n/g)
				.map((l) => l.trim())
				.filter((l) => l && !l.startsWith('#'));
			for (const line of lines) {
				if (!line.startsWith('file://')) continue;
				try {
					const url = new URL(line);
					const decoded = decodeURIComponent(url.pathname);
					if (decoded) paths.push(decoded);
				} catch {
					// ignore
				}
			}
		}
		return paths;
	}

	async function handleDropEvent(e: DragEvent) {
		e.preventDefault();
		e.stopPropagation();
		const dt = e.dataTransfer;
		const paths = await extractPathsFromDataTransfer(dt);
		if (paths.length === 0) return;
		void addFilesByPath(paths);
	}

	async function addFilesByPath(paths: string[]) {
		if (!window.convertable) return;
		const hadNoSelection = selected.size === 0;
		const unique = Array.from(new Set(paths.filter((p) => typeof p === 'string' && p)));
		if (unique.length === 0) return;
		const metas = await window.convertable.getFileMetadata(unique);
		for (const m of metas) {
			if (dropped.some((x) => x.path === m.path)) continue;
			dropped.push(m);
		}
		// UX: when users add multiple files (drop/pick), they typically expect Convert
		// to run on all of them without needing multi-select.
		if (dropped.length > 0 && hadNoSelection) {
			selected.clear();
			for (let i = 0; i < dropped.length; i++) {
				const item = dropped[i];
				if (item) selected.add(item.path);
			}
			lastSelectedIndex = dropped.length - 1;
		}
		updateConvertLayout();
		updateTargetOptionsAndConvertState();
		renderFiles();
		updateSelectionCount();
	}
	
	function renderFiles() {
		if (!fileList) return;
		const animateFromIndex = Math.max(0, Math.min(lastRenderedDroppedCount, dropped.length));
		lastRenderedDroppedCount = dropped.length;
		fileList.innerHTML = '';
		for (let idx = 0; idx < dropped.length; idx += 1) {
			const f = dropped[idx];
			if (!f) continue;
			const row = document.createElement('div');
			row.className = 'file-row';
			if (idx >= animateFromIndex) row.classList.add('animate-in');
			row.classList.toggle('selected', selected.has(f.path));
			const name = document.createElement('span');
			name.className = 'file-name';
			name.textContent = f.name;
			name.title = f.path;
			const size = document.createElement('span');
			size.className = 'file-size';
			size.textContent = humanSize(f.sizeBytes);
			const ext = document.createElement('span');
			ext.className = 'file-ext';
			ext.textContent = f.ext;
			ext.title = f.mime;
			const mime = document.createElement('span');
			mime.className = 'file-mime';
			mime.textContent = f.mime;

			row.addEventListener('click', (ev) => {
				const idx = dropped.findIndex((x) => x.path === f.path);
				const isMeta = (ev.metaKey || ev.ctrlKey) && !ev.shiftKey;
				if (ev.shiftKey && lastSelectedIndex >= 0) {
					const start = Math.min(lastSelectedIndex, idx);
					const end = Math.max(lastSelectedIndex, idx);
					if (!isMeta) selected.clear();
					for (let i = start; i <= end; i++) {
						const item = dropped[i];
						if (item) selected.add(item.path);
					}
				} else if (isMeta) {
					if (selected.has(f.path)) selected.delete(f.path);
					else selected.add(f.path);
					lastSelectedIndex = idx;
				} else {
					selected.clear();
					selected.add(f.path);
					lastSelectedIndex = idx;
				}
				renderFiles();
				updateTargetOptionsAndConvertState();
				updateSelectionCount();
			});

			row.append(name, size, mime, ext);
			fileList.appendChild(row);
		}
	}
	
	// (legacy) renderQueue removed in favor of renderWorkers
	
	function renderResults() {
		const resultList = document.getElementById('result-list');
		if (!resultList) return;
		const animateFromIndex = Math.max(0, Math.min(lastRenderedResultsCount, results.length));
		lastRenderedResultsCount = results.length;
		resultList.innerHTML = '';
		for (let idx = 0; idx < results.length; idx += 1) {
			const item = results[idx];
			if (!item) continue;
			if (resultsFilterText) {
				const hay = `${basename(item.outputPath)} ${item.targetExt} ${item.sourceName}`.toLowerCase();
				if (!hay.includes(resultsFilterText)) continue;
			}
			const row = document.createElement('div');
			row.className = 'result-row';
			if (idx >= animateFromIndex) row.classList.add('animate-in');
			row.draggable = true;
			const name = document.createElement('span');
			name.className = 'result-name';
			name.textContent = basename(item.outputPath);
			name.title = item.outputPath;
			const target = document.createElement('span');
			target.className = 'result-ext';
			target.textContent = item.targetExt;
			const reveal = document.createElement('button');
			reveal.type = 'button';
			reveal.textContent = 'Reveal';
			reveal.className = 'secondary small';
			reveal.draggable = false;
			reveal.addEventListener('click', (ev) => {
				ev.preventDefault();
				ev.stopPropagation();
				hideContextMenu();
				void window.convertable?.revealInFinder(item.outputPath);
			});

			row.addEventListener('contextmenu', (ev) => {
				ev.preventDefault();
				ev.stopPropagation();
				showContextMenu(ev.clientX, ev.clientY, idx);
			});

			row.addEventListener('dragstart', (ev) => {
				// Use Electron main-process drag-out so the item can be dropped into Finder/Desktop.
				// Prevent the default HTML5 drag payload (which can create a dropped text file).
				ev.preventDefault();
				ev.stopPropagation();
				try {
					window.convertable?.startDrag(item.outputPath);
				} catch {
					// ignore
				}
			});
			row.append(name, target, reveal);
			resultList.appendChild(row);
		}
	}

	// Result context menu wiring
	if (resultContextMenu) {
		document.addEventListener('click', (ev) => {
			if (!resultContextMenu || resultContextMenu.hasAttribute('hidden')) return;
			const target = ev.target as HTMLElement | null;
			if (target && resultContextMenu.contains(target)) return;
			hideContextMenu();
		});
		window.addEventListener('blur', hideContextMenu);
		window.addEventListener('resize', hideContextMenu);
		window.addEventListener('keydown', (ev) => {
			if (ev.key === 'Escape') hideContextMenu();
		});
		resultContextMenu.addEventListener('click', (ev) => {
			const target = ev.target as HTMLElement | null;
			if (!target) return;
			const btn = target.closest('button[data-action]') as HTMLButtonElement | null;
			if (!btn) return;
			const action = btn.dataset.action;
			const idx = contextMenuResultIndex;
			if (!action || idx == null) return;
			const item = results[idx];
			if (!item) return;
			hideContextMenu();
			void (async () => {
				try {
					if (action === 'reveal') {
						await window.convertable?.revealInFinder(item.outputPath);
						return;
					}
					if (action === 'rerun') {
						const job: ConversionJob = {
							sourcePath: item.sourcePath,
							sourceName: item.sourceName ?? basename(item.sourcePath),
							targetExt: item.targetExt,
							status: 'Queued',
							progress: 0,
						};
						const wid = chooseWorkerForNewJob();
						enqueueToWorker(job, wid);
						await window.convertable?.enqueueJobs([
							{ srcPath: job.sourcePath, targetExt: job.targetExt, workerId: wid },
						]);
						toast(`Queued: ${basename(job.sourcePath)} → ${job.targetExt}`, 'success');
						return;
					}
					if (action === 'copyPath') {
						await copyText(item.outputPath);
						toast('Copied output path', 'success');
						return;
					}
					if (action === 'copyName') {
						await copyText(basename(item.outputPath));
						toast('Copied filename', 'success');
						return;
					}
					if (action === 'remove') {
						results.splice(idx, 1);
						renderResults();
						return;
					}
				} catch (err) {
					const msg = err instanceof Error ? err.message : String(err);
					toast(msg || 'Action failed', 'error');
				}
			})();
		});
	}

	resultFilter?.addEventListener('input', () => {
		resultsFilterText = (resultFilter.value || '').trim().toLowerCase();
		renderResults();
	});
	
	function handleEngineEvent(ev: EngineEvent) {
		ensureWorkers(workerCount);
		const wid = Math.max(0, Math.min(workers.length - 1, ev.workerId));
		const w = workers[wid];
		if (!w) return;
		lastActiveWorkerId = wid;

		const k = jobKey(ev.srcPath, ev.targetExt);
		if (ev.type === 'start') {
			// Move from pending -> running
			const idx = w.pending.findIndex((j) => j.sourcePath === ev.srcPath && j.targetExt === ev.targetExt);
			const pendingJob = idx >= 0 ? w.pending.splice(idx, 1)[0] : undefined;
			w.running = pendingJob ?? {
				sourcePath: ev.srcPath,
				sourceName: basename(ev.srcPath),
				targetExt: ev.targetExt,
				workerId: wid,
				status: 'Processing',
				progress: 0,
			};
			w.running.status = 'Processing';
			w.running.progress = 0;
			jobProgressByKey.set(k, 0);
			currentJobKey = k;
			currentJobName = w.running.sourceName;
			currentJobProgress = 0;
		} else if (ev.type === 'progress') {
			if (w.running && w.running.sourcePath === ev.srcPath && w.running.targetExt === ev.targetExt) {
				w.running.progress = ev.progress;
				w.running.status = 'Processing';
			}
			jobProgressByKey.set(k, ev.progress);
			if (currentJobKey === k) currentJobProgress = ev.progress;
		} else if (ev.type === 'done') {
			if (w.running && w.running.sourcePath === ev.srcPath && w.running.targetExt === ev.targetExt) {
				w.running.status = 'Done';
				w.running.progress = 1;
			}
			results.push({
				sourcePath: ev.srcPath,
				sourceName: w.running?.sourceName ?? basename(ev.srcPath),
				outputPath: ev.outputPath,
				targetExt: ev.targetExt,
			});
			renderResults();
			w.running = null;
			runDone = Math.min(runTotal, runDone + 1);
			runSuccessCount += 1;
			jobProgressByKey.set(k, 1);
			if (runDone >= runTotal) runEndMs = Date.now();
			toast(`Finished: ${basename(ev.srcPath)} → ${ev.targetExt}`, 'success', 2400);
			maybeNotifyRunComplete();
		} else if (ev.type === 'error') {
			if (w.running && w.running.sourcePath === ev.srcPath && w.running.targetExt === ev.targetExt) {
				w.running.status = 'Error';
				w.running.error = ev.message;
			}
			runDone = Math.min(runTotal, runDone + 1);
			runErrorCount += 1;
			jobProgressByKey.set(k, 1);
			if (runDone >= runTotal) runEndMs = Date.now();
			toast(`Error: ${basename(ev.srcPath)} → ${ev.targetExt}`, 'error', 5200);
			maybeNotifyRunComplete();
		} else if (ev.type === 'canceled') {
			if (w.running && w.running.sourcePath === ev.srcPath && w.running.targetExt === ev.targetExt) {
				w.running = null;
			} else {
				const idx = w.pending.findIndex((j) => j.sourcePath === ev.srcPath && j.targetExt === ev.targetExt);
				if (idx >= 0) w.pending.splice(idx, 1);
			}
			runDone = runTotal > 0 ? Math.min(runTotal, runDone + 1) : runDone;
			runCanceledCount += 1;
			jobProgressByKey.set(k, 1);
			if (runTotal > 0 && runDone >= runTotal) runEndMs = Date.now();
			toast(`Canceled: ${basename(ev.srcPath)} → ${ev.targetExt}`, 'info');
			maybeNotifyRunComplete();
		}
		// Progress updates can be very frequent. Avoid rerendering worker queues while
		// the user is dragging a pending job, otherwise DnD gets interrupted.
		if (ev.type === 'progress' && (isDraggingPending || Date.now() < dragInteractionGuardUntil)) {
			updateQueueStats();
			updateConvertProgress();
			return;
		}
		scheduleRenderWorkers();
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
		e.stopPropagation();
		dropZone.classList.remove('drag-over');
		handleDropEvent(e);
	});

	fileList.addEventListener('dragover', (e) => {
		e.preventDefault();
		fileList.classList.add('drag-over');
	});

	fileList.addEventListener('dragleave', (e) => {
		e.preventDefault();
		fileList.classList.remove('drag-over');
	});

	fileList.addEventListener('drop', (e) => {
		e.preventDefault();
		e.stopPropagation();
		fileList.classList.remove('drag-over');
		handleDropEvent(e);
	});

	// Electron default behavior when dropping a file onto the window is to try
	// to navigate/open it. Prevent that and accept drops anywhere.
	const preventDragDefaults = (e: DragEvent) => {
		e.preventDefault();
		e.stopPropagation();
		if (e.dataTransfer) e.dataTransfer.dropEffect = 'copy';
	};
	window.addEventListener('dragenter', preventDragDefaults, { capture: true });
	window.addEventListener('dragover', preventDragDefaults, { capture: true });
	window.addEventListener('drop', (e) => {
		void handleDropEvent(e);
	}, { capture: true });

	pickButton?.addEventListener('click', () => {
		void (async () => {
			if (!window.convertable) return;
			const picked = await window.convertable.pickFiles();
			await addFilesByPath(picked.map((p) => p.path));
		})();
	});

	clearFilesButton?.addEventListener('click', () => {
		dropped.splice(0, dropped.length);
		selected.clear();
		lastSelectedIndex = -1;
		runTotal = 0;
		runDone = 0;
		currentJobKey = null;
		currentJobName = null;
		currentJobProgress = 0;
		runStartMs = null;
		runEndMs = null;
		runSuccessCount = 0;
		runErrorCount = 0;
		runCanceledCount = 0;
		runCompleteNotified = false;
		runJobKeys = [];
		jobProgressByKey.clear();
		jobBytesByKey.clear();
		ensureWorkers(workerCount);
		for (const w of workers) {
			w.running = null;
			w.pending.splice(0, w.pending.length);
		}
		updateConvertLayout();
		renderFiles();
		scheduleRenderWorkers();
		updateTargetOptionsAndConvertState();
		updateConvertProgress();
		updateQueueStats();
		updateSelectionCount();
	});

	clearResultsButton?.addEventListener('click', () => {
		results.splice(0, results.length);
		resultsFilterText = '';
		if (resultFilter) resultFilter.value = '';
		hideContextMenu();
		renderResults();
	});

	cancelAllButton?.addEventListener('click', () => {
		void (async () => {
			try {
				await window.convertable?.cancelAll?.();
				toast('Cancel all requested', 'info');
			} catch (err) {
				const msg = err instanceof Error ? err.message : String(err);
				toast(msg || 'Cancel failed', 'error');
			}
		})();
	});

	pauseButton?.addEventListener('click', () => {
		void (async () => {
			try {
				const api = window.convertable;
				if (!api?.setPaused) return;
				paused = await api.setPaused(!paused);
				scheduleRenderWorkers();
			} catch (err) {
				const msg = err instanceof Error ? err.message : String(err);
				toast(msg || 'Failed to toggle pause', 'error');
			}
		})();
	});

	workerInc?.addEventListener('click', () => {
		void (async () => {
			try {
				const api = window.convertable;
				if (!api?.setWorkerCount) return;
				const next = await api.setWorkerCount(workerCount + 1);
				ensureWorkers(next);
				scheduleRenderWorkers();
			} catch (err) {
				const msg = err instanceof Error ? err.message : String(err);
				toast(msg || 'Failed to add worker', 'error');
			}
		})();
	});

	workerDec?.addEventListener('click', () => {
		void (async () => {
			try {
				const api = window.convertable;
				if (!api?.setWorkerCount) return;
				const last = workers[Math.max(0, workerCount - 1)];
				if (workerCount <= 1) return;
				if (last?.running && last.running.status === 'Processing') {
					toast('Stop the last worker before removing it', 'info');
					return;
				}
				const next = await api.setWorkerCount(workerCount - 1);
				ensureWorkers(next);
				scheduleRenderWorkers();
				void syncPendingQueuesToEngine();
			} catch (err) {
				const msg = err instanceof Error ? err.message : String(err);
				toast(msg || 'Failed to remove worker', 'error');
			}
		})();
	});

	// Keyboard shortcuts
	window.addEventListener('keydown', (ev) => {
		const isMod = ev.metaKey || ev.ctrlKey;
		if (!isMod) return;
		const key = ev.key.toLowerCase();
		if (key === 'o') {
			ev.preventDefault();
			pickButton?.click();
		} else if (key === 'enter') {
			ev.preventDefault();
			convertButton?.click();
		} else if (key === 'k') {
			ev.preventDefault();
			clearResultsButton?.click();
		}
	});
	
	convertButton.addEventListener('click', async () => {
		if (!window.convertable) {
			// Backend not wired yet.
			return;
		}
		if (convertButton.disabled) return;
		const targetExt = targetSelect.value.trim();
		if (!targetExt) return;
		const srcPaths = selected.size > 0 ? Array.from(selected) : dropped.map((f) => f.path);
		const jobs = srcPaths.map<ConversionJob>((p) => {
			const f = dropped.find((x) => x.path === p);
			return {
				sourcePath: f?.path ?? p,
				sourceName: f?.name ?? basename(p),
				targetExt,
				status: 'Queued',
				progress: 0,
			};
		});

		const enq: { srcPath: string; targetExt: string; workerId?: number }[] = [];
		for (const job of jobs) {
			const wid = chooseWorkerForNewJob();
			enqueueToWorker(job, wid);
			enq.push({ srcPath: job.sourcePath, targetExt: job.targetExt, workerId: wid });
		}
		await window.convertable.enqueueJobs(enq);
	});
	
	if (window.convertable) {
		window.convertable.onEngineEvent(handleEngineEvent);
	}
	async function refreshEngineState() {
		const api = window.convertable;
		if (!api) return;
		try {
			if (api.getWorkerCount) {
				workerCount = await api.getWorkerCount();
				ensureWorkers(workerCount);
			}
			if (api.getPaused) {
				paused = await api.getPaused();
			}
			scheduleRenderWorkers();
		} catch {
			// ignore
		}
	}
	// Initialize output dir UI.
	renderOutputDir();
	void refreshOutputDir();
	void refreshCpuThreads();
	void refreshEngineState();

	updateConvertLayout();
	updateTargetOptionsAndConvertState();
	updateConvertProgress();
	scheduleRenderWorkers();
	updateQueueStats();
	updateSelectionCount();
}

window.addEventListener('DOMContentLoaded', () => {
	const setActiveTab = setupTabs();
	setupConvertTab();

	// Tab shortcuts live at the document level.
	window.addEventListener('keydown', (ev) => {
		const isMod = ev.metaKey || ev.ctrlKey;
		if (!isMod) return;
		const key = ev.key;
		if (key === '1') {
			ev.preventDefault();
			setActiveTab('convert');
		} else if (key === '2') {
			ev.preventDefault();
			setActiveTab('queue');
		} else if (key === '3') {
			ev.preventDefault();
			setActiveTab('result');
		} else if (key === '4') {
			ev.preventDefault();
			setActiveTab('settings');
		}
	});
});
