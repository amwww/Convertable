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
	getOutputDir(): Promise<{ configured: string | null; effective: string }>;
	pickOutputDir(): Promise<{ configured: string | null; effective: string; canceled: boolean }>;
	resetOutputDir(): Promise<{ configured: string | null; effective: string }>;
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
	
	if (!dropZone || !fileList || !targetSelect || !convertButton) return;
	
	const dropped: DroppedFile[] = [];
	const queue: ConversionJob[] = [];
	const results: ConversionResultItem[] = [];
	const selected = new Set<string>();
	let lastSelectedIndex = -1;

	let lastRenderedDroppedCount = 0;
	let lastRenderedQueueCount = 0;
	let lastRenderedResultsCount = 0;

	let runTotal = 0;
	let runDone = 0;
	let currentJobKey: string | null = null;
	let currentJobName: string | null = null;
	let currentJobProgress = 0;
	let runStartMs: number | null = null;
	let runJobKeys: string[] = [];
	const jobProgressByKey = new Map<string, number>();
	const jobBytesByKey = new Map<string, number>();
	let hideProgressTimer: number | null = null;
	let resultsFilterText = '';
	let outputDirEffective: string | null = null;
	let outputDirConfigured: string | null = null;

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

		const overall = Math.max(
			0,
			Math.min(1, (runDone + (currentJobKey ? currentJobProgress : 0)) / runTotal),
		);
		const pct = Math.round(overall * 100);
		const parts: string[] = [];
		parts.push(`${pct}%`);
		parts.push(`${runDone + 1}/${runTotal}`);
		if (currentJobName) parts.push(currentJobName);
		convertProgressStatus.textContent = parts.join(' — ');
		convertProgressFill.style.width = `${pct}%`;
	}

	function jobKey(srcPath: string, targetExt: string): string {
		return `${srcPath}::${targetExt}`;
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
		const elEta = document.getElementById('stat-eta');
		const elCurrent = document.getElementById('stat-current');
		const elBytes = document.getElementById('stat-bytes');
		const elJobs = document.getElementById('stat-jobs');
		if (!elOverall || !elEta || !elCurrent || !elBytes || !elJobs) return;

		const running = runTotal > 0 && runDone < runTotal;
		const p = overallProgress();
		elOverall.textContent = `${Math.round(p * 100)}%`;
		elJobs.textContent = `${Math.min(runDone, runTotal)}/${runTotal}`;

		if (!running) {
			elEta.textContent = '—';
			elCurrent.textContent = 'No jobs running';
		} else {
			elCurrent.textContent = currentJobName
				? `${currentJobName} (${Math.round(currentJobProgress * 100)}%)`
				: '—';
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
	
	function renderQueue() {
		const queueList = document.getElementById('queue-list');
		if (!queueList) return;
		const animateFromIndex = Math.max(0, Math.min(lastRenderedQueueCount, queue.length));
		lastRenderedQueueCount = queue.length;
		queueList.innerHTML = '';
		if (queue.length === 0) {
			const empty = document.createElement('div');
			empty.className = 'empty-state';
			empty.textContent = 'No jobs running';
			queueList.appendChild(empty);
			updateQueueStats();
			return;
		}
		for (let idx = 0; idx < queue.length; idx += 1) {
			const job = queue[idx];
			if (!job) continue;
			const row = document.createElement('div');
			row.className = 'queue-row';
			if (idx >= animateFromIndex) row.classList.add('animate-in');
			row.classList.toggle('is-running', job.status === 'Processing');
			const progressWrap = document.createElement('div');
			progressWrap.className = 'queue-progress';
			const progressFill = document.createElement('div');
			progressFill.className = 'queue-progress__fill';
			const p = typeof job.progress === 'number' ? Math.max(0, Math.min(1, job.progress)) : 0;
			progressFill.style.width = `${(p * 100).toFixed(1)}%`;
			progressWrap.appendChild(progressFill);
			row.appendChild(progressWrap);
			const name = document.createElement('span');
			name.className = 'queue-name';
			name.textContent = job.sourceName;
			const status = document.createElement('span');
			status.className = 'queue-status';
			const pct = typeof job.progress === 'number' ? ` ${(job.progress * 100).toFixed(0)}%` : '';
			status.textContent = `${job.status}${pct}`;
			status.title = job.error ?? '';
			row.append(name, status);
			queueList.appendChild(row);
		}
		updateQueueStats();
	}
	
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
				void window.convertable?.revealInFinder(item.outputPath);
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

	resultFilter?.addEventListener('input', () => {
		resultsFilterText = (resultFilter.value || '').trim().toLowerCase();
		renderResults();
	});
	
	function handleEngineEvent(ev: EngineEvent) {
		if (ev.type === 'start') {
			const job = queue.find(
				(j) => j.sourcePath === ev.srcPath && j.targetExt === ev.targetExt,
			);
			if (job) {
				job.status = 'Processing';
				job.progress = 0;
				renderQueue();
			}
			currentJobKey = jobKey(ev.srcPath, ev.targetExt);
			currentJobName = job?.sourceName ?? basename(ev.srcPath);
			currentJobProgress = 0;
			jobProgressByKey.set(jobKey(ev.srcPath, ev.targetExt), 0);
			updateConvertProgress();
			updateQueueStats();
		} else if (ev.type === 'progress') {
			const job = queue.find(
				(j) => j.sourcePath === ev.srcPath && j.targetExt === ev.targetExt,
			);
			if (job) {
				job.progress = ev.progress;
				renderQueue();
			}
			const key = `${ev.srcPath}::${ev.targetExt}`;
			if (currentJobKey === key) {
				currentJobProgress = ev.progress;
				updateConvertProgress();
			}
			jobProgressByKey.set(jobKey(ev.srcPath, ev.targetExt), ev.progress);
			updateQueueStats();
		} else if (ev.type === 'done') {
			const idx = queue.findIndex(
				(j) => j.sourcePath === ev.srcPath && j.targetExt === ev.targetExt,
			);
			const job = idx >= 0 ? queue[idx] : undefined;
			if (job) {
				job.status = 'Done';
				job.progress = 1;
			}
			results.push({
				sourcePath: ev.srcPath,
				sourceName: job?.sourceName ?? ev.srcPath,
				outputPath: ev.outputPath,
				targetExt: ev.targetExt,
			});
			renderResults();

			// Auto-remove finished jobs from the Queue list.
			if (idx >= 0) queue.splice(idx, 1);
			renderQueue();

			runDone = Math.min(runTotal, runDone + 1);
			currentJobKey = null;
			currentJobName = null;
			currentJobProgress = 0;
			updateConvertProgress();
			jobProgressByKey.set(jobKey(ev.srcPath, ev.targetExt), 1);
			if (runDone >= runTotal) runStartMs = null;
			updateQueueStats();
		} else if (ev.type === 'error') {
			const job = queue.find(
				(j) => j.sourcePath === ev.srcPath && j.targetExt === ev.targetExt,
			);
			if (job) {
				job.status = `Error`;
				job.error = ev.message;
				renderQueue();
			}
			// Treat errors as finished for overall progress, but keep them visible in Queue.
			runDone = Math.min(runTotal, runDone + 1);
			currentJobKey = null;
			currentJobName = null;
			currentJobProgress = 0;
			updateConvertProgress();
			jobProgressByKey.set(jobKey(ev.srcPath, ev.targetExt), 1);
			if (runDone >= runTotal) runStartMs = null;
			updateQueueStats();
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
		runJobKeys = [];
		jobProgressByKey.clear();
		jobBytesByKey.clear();
		queue.splice(0, queue.length);
		updateConvertLayout();
		renderFiles();
		renderQueue();
		updateTargetOptionsAndConvertState();
		updateConvertProgress();
		updateQueueStats();
		updateSelectionCount();
	});

	clearResultsButton?.addEventListener('click', () => {
		results.splice(0, results.length);
		resultsFilterText = '';
		if (resultFilter) resultFilter.value = '';
		renderResults();
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
		queue.splice(0, queue.length, ...jobs);
		renderQueue();

		runStartMs = Date.now();
		runJobKeys = jobs.map((j) => jobKey(j.sourcePath, j.targetExt));
		jobProgressByKey.clear();
		jobBytesByKey.clear();
		for (const j of jobs) {
			const k = jobKey(j.sourcePath, j.targetExt);
			jobProgressByKey.set(k, 0);
			const meta = dropped.find((x) => x.path === j.sourcePath);
			if (meta?.sizeBytes != null) jobBytesByKey.set(k, meta.sizeBytes);
		}

		clearHideProgressTimer();
		runTotal = jobs.length;
		runDone = 0;
		currentJobKey = null;
		currentJobName = null;
		currentJobProgress = 0;
		updateConvertProgress();
		updateQueueStats();
		
		await window.convertable.enqueueJobs(
			jobs.map((j) => ({ srcPath: j.sourcePath, targetExt: j.targetExt })),
		);
	});
	
	if (window.convertable) {
		window.convertable.onEngineEvent(handleEngineEvent);
	}
	// Initialize output dir UI.
	renderOutputDir();
	void refreshOutputDir();

	updateConvertLayout();
	updateTargetOptionsAndConvertState();
	updateConvertProgress();
	renderQueue();
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
		}
	});
});
