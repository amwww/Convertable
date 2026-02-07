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
	if (bytes == null) return '—';
	const mb = bytes / (1024 * 1024);
	return `${mb.toFixed(2)} MB`;
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
	const selected = new Set<string>();
	let lastSelectedIndex = -1;

	let runTotal = 0;
	let runDone = 0;
	let currentJobKey: string | null = null;
	let currentJobName: string | null = null;
	let currentJobProgress = 0;
	let hideProgressTimer: number | null = null;

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
		if (allowed.includes(prev)) targetSelect.value = prev;
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
	}
	
	function renderFiles() {
		if (!fileList) return;
		fileList.innerHTML = '';
		for (const f of dropped) {
			const row = document.createElement('div');
			row.className = 'file-row';
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
			});

			row.append(name, size, mime, ext);
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
	}
	
	function renderResults() {
		const resultList = document.getElementById('result-list');
		if (!resultList) return;
		resultList.innerHTML = '';
		for (const item of results) {
			const row = document.createElement('div');
			row.className = 'result-row';
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
			currentJobKey = `${ev.srcPath}::${ev.targetExt}`;
			currentJobName = job?.sourceName ?? basename(ev.srcPath);
			currentJobProgress = 0;
			updateConvertProgress();
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

		clearHideProgressTimer();
		runTotal = jobs.length;
		runDone = 0;
		currentJobKey = null;
		currentJobName = null;
		currentJobProgress = 0;
		updateConvertProgress();
		
		await window.convertable.enqueueJobs(
			jobs.map((j) => ({ srcPath: j.sourcePath, targetExt: j.targetExt })),
		);
	});
	
	if (window.convertable) {
		window.convertable.onEngineEvent(handleEngineEvent);
	}

	updateConvertLayout();
	updateTargetOptionsAndConvertState();
	updateConvertProgress();
}

window.addEventListener('DOMContentLoaded', () => {
	setupTabs();
	setupConvertTab();
});
