import { app, BrowserWindow, dialog, ipcMain, nativeImage, shell } from 'electron';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';

import { lookup as mimeLookup } from 'mime-types';
import sharp from 'sharp';

import type { EngineEvent } from './models.js';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

type EnqueueJob = { srcPath: string; targetExt: string };

type DroppedFile = {
	path: string;
	name: string;
	sizeBytes: number | null;
	mime: string;
	ext: string;
};

function extUpper(filePath: string): string {
	const ext = path.extname(filePath);
	return ext ? ext.toUpperCase() : '—';
}

async function statSize(filePath: string): Promise<number | null> {
	try {
		const st = await fs.stat(filePath);
		return typeof st.size === 'number' ? st.size : null;
	} catch {
		return null;
	}
}

function detectMime(filePath: string): string {
	const mt = mimeLookup(filePath);
	if (typeof mt === 'string' && mt) return mt;
	return 'application/octet-stream';
}

async function toDroppedFile(filePath: string): Promise<DroppedFile> {
	return {
		path: filePath,
		name: path.basename(filePath),
		sizeBytes: await statSize(filePath),
		mime: detectMime(filePath),
		ext: extUpper(filePath),
	};
}

let sessionOutputDir: string | null = null;
let sessionOutputDirPromise: Promise<string> | null = null;

async function defaultOutputDir(): Promise<string> {
	if (sessionOutputDir) return sessionOutputDir;
	if (sessionOutputDirPromise) return sessionOutputDirPromise;

	// Store outputs in a temp folder (like the old app behavior).
	// This also avoids cluttering Downloads during testing.
	sessionOutputDirPromise = (async () => {
		const base = path.join(os.tmpdir(), 'convertable-');
		const dir = await fs.mkdtemp(base);
		sessionOutputDir = dir;
		return dir;
	})();

	return sessionOutputDirPromise;
}

async function ensureDir(dirPath: string): Promise<void> {
	await fs.mkdir(dirPath, { recursive: true });
}

function withNewExtension(srcPath: string, targetExt: string, outDir: string): string {
	const parsed = path.parse(srcPath);
	const cleanExt = targetExt.startsWith('.') ? targetExt : `.${targetExt}`;
	return path.join(outDir, `${parsed.name}${cleanExt.toLowerCase()}`);
}

function isImageTarget(ext: string): boolean {
	return ['.PNG', '.JPEG', '.JPG', '.WEBP'].includes(ext.toUpperCase());
}

function isMediaTarget(ext: string): boolean {
	return ['.MP3', '.WAV', '.M4A', '.MP4', '.MOV'].includes(ext.toUpperCase());
}

async function convertImage(srcPath: string, destPath: string, targetExt: string): Promise<void> {
	const t = targetExt.toUpperCase();
	const img = sharp(srcPath);
	if (t === '.PNG') {
		await img.png().toFile(destPath);
		return;
	}
	if (t === '.WEBP') {
		await img.webp({ quality: 90 }).toFile(destPath);
		return;
	}
	// .JPEG or .JPG
	await img.jpeg({ quality: 92 }).toFile(destPath);
}

async function spawnFfmpeg(args: string[]): Promise<void> {
	const cmd = await ffmpegCommand();
	return new Promise((resolve, reject) => {
		const child = spawn(cmd, args, { stdio: ['ignore', 'ignore', 'pipe'] });
		let stderr = '';
		child.stderr.on('data', (buf) => {
			stderr += buf.toString('utf-8');
		});
		child.on('error', (err) => {
			// Common case: ffmpeg isn't installed / not on PATH and we couldn't load the bundled binary.
			if (err && typeof err === 'object' && 'code' in err && (err as any).code === 'ENOENT') {
				reject(
					new Error(
						'ffmpeg was not found. Install it (macOS: `brew install ffmpeg`) or bundle it via @ffmpeg-installer/ffmpeg.',
					),
				);
				return;
			}
			reject(err);
		});
		child.on('close', (code) => {
			if (code === 0) resolve();
			reject(new Error(stderr || `ffmpeg exited with code ${code}`));
		});
	});
}

let cachedFfmpegCommand: string | null = null;
async function ffmpegCommand(): Promise<string> {
	if (cachedFfmpegCommand) return cachedFfmpegCommand;
	try {
		const mod = (await import('@ffmpeg-installer/ffmpeg')) as unknown as {
			path?: unknown;
			default?: { path?: unknown };
		};
		const p: unknown = mod.path ?? mod.default?.path;
		if (typeof p === 'string' && p) {
			cachedFfmpegCommand = p;
			return cachedFfmpegCommand;
		}
	} catch {
		// ignore
	}
	cachedFfmpegCommand = 'ffmpeg';
	return cachedFfmpegCommand;
}

async function convertWithFfmpeg(srcPath: string, destPath: string, targetExt: string): Promise<void> {
	const t = targetExt.toUpperCase();
	const args: string[] = ['-y', '-i', srcPath];
	if (t === '.MP3') {
		args.push('-vn', '-c:a', 'libmp3lame', '-q:a', '2', destPath);
		await spawnFfmpeg(args);
		return;
	}
	if (t === '.WAV') {
		args.push('-vn', '-c:a', 'pcm_s16le', destPath);
		await spawnFfmpeg(args);
		return;
	}
	if (t === '.M4A') {
		args.push('-vn', '-c:a', 'aac', '-b:a', '192k', destPath);
		await spawnFfmpeg(args);
		return;
	}
	if (t === '.MP4') {
		args.push('-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '192k', destPath);
		await spawnFfmpeg(args);
		return;
	}
	// .MOV (simple h264-in-mov)
	args.push('-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '192k', destPath);
	await spawnFfmpeg(args);
}

class ConversionEngine {
	private pending: EnqueueJob[] = [];
	private processing = false;
	private readonly emit: (event: EngineEvent) => void;
	
	constructor(emit: (event: EngineEvent) => void) {
		this.emit = emit;
	}
	
	enqueueJobs(jobs: EnqueueJob[]): void {
		this.pending.push(...jobs);
		this.maybeStartNext();
	}
	
	private maybeStartNext(): void {
		if (this.processing) return;
		const job = this.pending.shift();
		if (!job) return;
		this.processing = true;
		const { srcPath, targetExt } = job;

		void this.runOne(srcPath, targetExt)
			.catch(() => {
				// runOne already emits error
			})
			.finally(() => {
				this.processing = false;
				this.maybeStartNext();
			});
	}

	private async runOne(srcPath: string, targetExt: string): Promise<void> {
		this.emit({ type: 'start', srcPath, targetExt });
		this.emit({ type: 'progress', srcPath, targetExt, progress: 0 });

		// Pseudo-progress: many backends don't provide native progress. We emit a
		// smooth curve up to 95% while work is ongoing, then snap to 100%.
		const startedAt = Date.now();
		let lastEmitted = 0;
		const tickMs = 120;
		const timer = setInterval(() => {
			const elapsed = Date.now() - startedAt;
			// Ease-out curve that approaches 0.95 asymptotically.
			const p = Math.min(0.95, 0.95 * (1 - Math.exp(-elapsed / 1200)));
			if (p > lastEmitted + 0.005) {
				lastEmitted = p;
				this.emit({ type: 'progress', srcPath, targetExt, progress: p });
			}
		}, tickMs);

		const outDir = defaultOutputDir();
		const outDirResolved = await outDir;
		await ensureDir(outDirResolved);
		const outputPath = withNewExtension(srcPath, targetExt, outDirResolved);

		try {
			if (isImageTarget(targetExt)) {
				await convertImage(srcPath, outputPath, targetExt);
			} else if (isMediaTarget(targetExt)) {
				await convertWithFfmpeg(srcPath, outputPath, targetExt);
			} else {
				throw new Error(`Unsupported target: ${targetExt}`);
			}
			clearInterval(timer);
			this.emit({ type: 'progress', srcPath, targetExt, progress: 1 });
			this.emit({ type: 'done', srcPath, outputPath, targetExt });
		} catch (err) {
			clearInterval(timer);
			const msg = err instanceof Error ? err.message : String(err);
			this.emit({ type: 'error', srcPath, targetExt, message: msg });
			throw err;
		}
	}
}

const engine = new ConversionEngine((event: EngineEvent) => {
	for (const win of BrowserWindow.getAllWindows()) {
		win.webContents.send('engine/event', event);
	}
});

function createWindow() {
	const isMac = process.platform === 'darwin';
	const win = new BrowserWindow({
		width: 800,
		height: 600,
		backgroundColor: '#020617',
		titleBarStyle: isMac ? 'hiddenInset' : undefined,
		webPreferences: {
			// Use a CJS preload to avoid ESM preload compatibility issues.
			preload: path.join(__dirname, '../preload.cjs'),
			contextIsolation: true,
			nodeIntegration: false,
		},
	});
	
	// index.html lives one level up from dist/
	const htmlPath = path.join(__dirname, '../index.html');
	win.loadFile(htmlPath);
	
	win.webContents.on('did-finish-load', () => {
		console.log('Renderer loaded:', htmlPath);
		win.webContents
			.executeJavaScript(
				`(() => ({ hasConvertable: !!window.convertable, keys: window.convertable ? Object.keys(window.convertable) : [] }))()`,
			)
			.then((info) => console.log('Preload API:', info))
			.catch((err) => console.warn('Preload API check failed:', err));
	});
	
	win.webContents.on('did-fail-load', (_event, errorCode, errorDescription) => {
		console.error('Renderer failed to load:', htmlPath, errorCode, errorDescription);
	});

	win.webContents.on('will-navigate', (event, url) => {
		// Allow reloads / navigation within our own app, but block attempts to
		// navigate to arbitrary dropped files (common Electron DnD behavior).
		try {
			const target = new URL(url);
			const isIndex = target.protocol === 'file:' && path.posix.basename(target.pathname) === 'index.html';
			const isSameAsCurrent = url === win.webContents.getURL();
			if (isIndex || isSameAsCurrent) return;
		} catch {
			// If parsing fails, treat it as suspicious.
		}
		console.warn('Blocked navigation attempt:', url);
		event.preventDefault();
	});

	win.webContents.setWindowOpenHandler(({ url }) => {
		console.warn('Blocked new-window attempt:', url);
		return { action: 'deny' };
	});
}

app.whenReady().then(() => {
	ipcMain.on('files/startDrag', (event, args: { path: string }) => {
		const p = typeof args?.path === 'string' ? args.path : '';
		if (!p) return;
		let icon = nativeImage.createEmpty();
		try {
			// If the file is an image, this gives a nice drag preview.
			const candidate = nativeImage.createFromPath(p);
			if (!candidate.isEmpty()) icon = candidate;
		} catch {
			// ignore
		}
		try {
			event.sender.startDrag({ file: p, icon });
		} catch {
			// ignore
		}
	});

	ipcMain.handle('files/pick', async () => {
		const res = await dialog.showOpenDialog({
			properties: ['openFile', 'multiSelections'],
		});
		if (res.canceled) return [] satisfies DroppedFile[];
		const items = await Promise.all(res.filePaths.map((p) => toDroppedFile(p)));
		return items;
	});

	ipcMain.handle('files/metadata', async (_event, args: { paths: string[] }) => {
		const paths = Array.isArray(args?.paths) ? args.paths : [];
		const unique = Array.from(new Set(paths.filter((p) => typeof p === 'string' && p)));
		const items = await Promise.all(unique.map((p) => toDroppedFile(p)));
		return items;
	});

	ipcMain.handle('files/reveal', async (_event, args: { path: string }) => {
		const p = typeof args?.path === 'string' ? args.path : '';
		if (!p) return;
		try {
			shell.showItemInFolder(p);
		} catch {
			// ignore
		}
	});

	ipcMain.handle('engine/enqueue', async (_event, args: { jobs: EnqueueJob[] }) => {
		engine.enqueueJobs(args.jobs ?? []);
	});
	
	createWindow();
	
	app.on('activate', () => {
		if (BrowserWindow.getAllWindows().length === 0) {
			createWindow();
		}
	});
});

app.on('window-all-closed', () => {
	if (process.platform !== 'darwin') {
		app.quit();
	}
});
