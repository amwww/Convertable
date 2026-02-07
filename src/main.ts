import { app, BrowserWindow, dialog, ipcMain, nativeImage, shell } from 'electron';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';

import Store from 'electron-store';

import { lookup as mimeLookup } from 'mime-types';
import sharp from 'sharp';
import JSZip from 'jszip';
import { PDFDocument } from 'pdf-lib';
import AdmZip from 'adm-zip';
import * as tar from 'tar';
import { createExtractorFromData } from 'node-unrar-js';

import type { EngineEvent } from './models.js';

type SevenZipBinModule = {
	path7za?: unknown;
	default?: { path7za?: unknown };
};

function setupProcessSignalHandlers() {
	// When running under nodemon, restarts are done via SIGTERM.
	// On macOS, Electron apps may not quit just because all windows close,
	// so explicitly call app.quit() on termination signals.
	let shuttingDown = false;
	const shutdown = () => {
		if (shuttingDown) return;
		shuttingDown = true;
		try {
			app.quit();
		} catch {
			// ignore
		}
	};

	process.on('SIGTERM', shutdown);
	process.on('SIGINT', shutdown);
}

setupProcessSignalHandlers();

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

const compoundExts = [
	'.tar.gz',
	'.tar.bz2',
	'.tar.xz',
	'.tar.zst',
	'.tar.lz',
	'.tar.lz4',
	'.tgz',
];

function normalizedExt(filePath: string): string {
	const baseLower = path.basename(filePath).toLowerCase();
	for (const ext of compoundExts) {
		if (baseLower.endsWith(ext)) return ext.toUpperCase();
	}
	const ext = path.extname(filePath);
	return ext ? ext.toUpperCase() : '—';
}

function baseNameWithoutKnownExt(filePath: string): string {
	const base = path.basename(filePath);
	const baseLower = base.toLowerCase();
	for (const ext of compoundExts) {
		if (baseLower.endsWith(ext)) return base.slice(0, Math.max(0, base.length - ext.length));
	}
	const parsed = path.parse(base);
	return parsed.name;
}

function extUpper(filePath: string): string {
	return normalizedExt(filePath);
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
	if (path.extname(filePath).toLowerCase() === '.pdf') return 'application/pdf';
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

type Settings = {
	outputDir: string | null;
};

let settingsStore: Store<Settings> | null = null;

function getSettingsStore(): Store<Settings> {
	if (settingsStore) return settingsStore;
	settingsStore = new Store<Settings>({
		name: 'settings',
		defaults: {
			outputDir: null,
		},
		// Schema is optional but helps keep the file sane.
		schema: {
			outputDir: {
				type: ['string', 'null'],
			},
		} as any,
	});
	return settingsStore;
}

function normalizeConfiguredOutputDir(value: unknown): string | null {
	if (typeof value !== 'string') return null;
	const trimmed = value.trim();
	if (!trimmed) return null;
	if (!path.isAbsolute(trimmed)) return null;
	return trimmed;
}

async function getConfiguredOutputDir(): Promise<string | null> {
	try {
		return normalizeConfiguredOutputDir(getSettingsStore().get('outputDir'));
	} catch {
		return null;
	}
}

async function setConfiguredOutputDir(dirPath: string | null): Promise<string | null> {
	if (dirPath == null) {
		getSettingsStore().set('outputDir', null);
		return null;
	}
	const normalized = normalizeConfiguredOutputDir(dirPath);
	if (!normalized) {
		throw new Error('Invalid output folder.');
	}
	await ensureDir(normalized);
	getSettingsStore().set('outputDir', normalized);
	return normalized;
}

async function resolveEffectiveOutputDir(): Promise<{ configured: string | null; effective: string }> {
	const configured = await getConfiguredOutputDir();
	if (configured) {
		await ensureDir(configured);
		return { configured, effective: configured };
	}
	const effective = await defaultOutputDir();
	await ensureDir(effective);
	return { configured: null, effective };
}

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
	const baseName = baseNameWithoutKnownExt(srcPath);
	const cleanExt = targetExt.startsWith('.') ? targetExt : `.${targetExt}`;
	return path.join(outDir, `${baseName}${cleanExt.toLowerCase()}`);
}

async function fileExists(filePath: string): Promise<boolean> {
	try {
		await fs.access(filePath);
		return true;
	} catch {
		return false;
	}
}

async function buildNonCollidingOutputPath(srcPath: string, targetExt: string, outDir: string): Promise<string> {
	const baseName = baseNameWithoutKnownExt(srcPath);
	const cleanExt = targetExt.startsWith('.') ? targetExt : `.${targetExt}`;
	const base = path.join(outDir, `${baseName}${cleanExt.toLowerCase()}`);
	if (!(await fileExists(base))) return base;
	for (let n = 1; n < 10_000; n += 1) {
		const candidate = path.join(outDir, `${baseName} (${n})${cleanExt.toLowerCase()}`);
		if (!(await fileExists(candidate))) return candidate;
	}
	// Extremely unlikely; fall back to timestamp.
	return path.join(outDir, `${baseName} (${Date.now()})${cleanExt.toLowerCase()}`);
}

async function buildNonCollidingOutputDir(srcPath: string, suffix: string, outDir: string): Promise<string> {
	const baseName = baseNameWithoutKnownExt(srcPath);
	const base = path.join(outDir, `${baseName}${suffix}`);
	if (!(await fileExists(base))) return base;
	for (let n = 1; n < 10_000; n += 1) {
		const candidate = path.join(outDir, `${baseName}${suffix} (${n})`);
		if (!(await fileExists(candidate))) return candidate;
	}
	return path.join(outDir, `${baseName}${suffix} (${Date.now()})`);
}

function isImageTarget(ext: string): boolean {
	return ['.PNG', '.JPEG', '.JPG', '.WEBP'].includes(ext.toUpperCase());
}

function isMediaTarget(ext: string): boolean {
	return ['.MP3', '.WAV', '.M4A', '.MP4', '.MOV'].includes(ext.toUpperCase());
}

function isArchiveTarget(ext: string): boolean {
	const t = ext.toUpperCase();
	return ['.ZIP', '.TAR', '.TAR.GZ', '.TGZ', '.7Z'].includes(t);
}

function isExtractTarget(ext: string): boolean {
	return ext.toUpperCase() === '.EXTRACT';
}

function isArchiveSourceExt(ext: string): boolean {
	const t = ext.toUpperCase();
	return ['.ZIP', '.TAR', '.TAR.GZ', '.TGZ', '.RAR', '.7Z'].includes(t);
}

function isSafeArchiveMemberPath(memberPath: string): boolean {
	// Avoid Zip Slip / path traversal when extracting.
	const p = memberPath.replace(/\\/g, '/');
	if (!p || p.startsWith('/') || /^[A-Za-z]:\//.test(p)) return false;
	const norm = path.posix.normalize(p);
	if (norm === '.' || norm.startsWith('../') || norm.includes('/../')) return false;
	return true;
}

async function listFilesRecursive(rootDir: string): Promise<string[]> {
	const out: string[] = [];
	async function walk(dir: string) {
		const entries = await fs.readdir(dir, { withFileTypes: true });
		for (const ent of entries) {
			const full = path.join(dir, ent.name);
			if (ent.isDirectory()) {
				await walk(full);
			} else if (ent.isFile()) {
				out.push(path.relative(rootDir, full));
			}
		}
	}
	await walk(rootDir);
	return out;
}

async function extractArchive(srcPath: string, destDir: string): Promise<void> {
	const srcExt = normalizedExt(srcPath).toUpperCase();
	await ensureDir(destDir);
	if (srcExt === '.ZIP') {
		const zip = new AdmZip(srcPath);
		for (const entry of zip.getEntries()) {
			const name = entry.entryName;
			if (!isSafeArchiveMemberPath(name)) continue;
			const outPath = path.join(destDir, name);
			if (entry.isDirectory) {
				await ensureDir(outPath);
				continue;
			}
			await ensureDir(path.dirname(outPath));
			const data = entry.getData();
			await fs.writeFile(outPath, data);
		}
		return;
	}
	if (srcExt === '.TAR' || srcExt === '.TAR.GZ' || srcExt === '.TGZ') {
		const gzip = srcExt === '.TAR.GZ' || srcExt === '.TGZ';
		await tar.x({
			file: srcPath,
			cwd: destDir,
			gzip,
			filter: (p) => isSafeArchiveMemberPath(p),
		});
		return;
	}
	if (srcExt === '.RAR') {
		const size = await statSize(srcPath);
		// node-unrar-js is in-memory; avoid OOM on very large RARs.
		if (size != null && size > 512 * 1024 * 1024) {
			throw new Error('RAR archive is too large to extract in this build (in-memory extractor).');
		}
		const bytes = await fs.readFile(srcPath);
		const ab = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
		const extractor = await createExtractorFromData({ data: ab });
		const res = extractor.extract();
		for (const f of res.files) {
			if (f.fileHeader.flags.directory) continue;
			const fileName = f.fileHeader.name;
			const data = f.extraction;
			if (!fileName || !data) continue;
			if (!isSafeArchiveMemberPath(fileName)) continue;
			const outPath = path.join(destDir, fileName);
			await ensureDir(path.dirname(outPath));
			await fs.writeFile(outPath, Buffer.from(data));
		}
		return;
	}
	if (srcExt === '.7Z') {
		const cmd = await sevenZipCommand();
		// Validate member paths before extracting.
		const listed = await spawnCommandCapture(cmd, ['l', '-slt', srcPath]);
		for (const line of listed.stdout.split(/\r?\n/g)) {
			const m = /^Path\s*=\s*(.*)$/.exec(line);
			if (!m) continue;
			const member = (m[1] ?? '').trim();
			if (!member) continue;
			if (!isSafeArchiveMemberPath(member)) {
				throw new Error('Archive contains unsafe paths and cannot be extracted.');
			}
		}
		await spawnCommand(cmd, ['x', '-y', `-o${destDir}`, srcPath]);
		return;
	}
	throw new Error(`Unsupported archive type: ${srcExt}`);
}

async function createArchiveFromDir(srcDir: string, destArchivePath: string, targetExt: string): Promise<void> {
	const t = targetExt.toUpperCase();
	if (t === '.ZIP') {
		const zip = new AdmZip();
		zip.addLocalFolder(srcDir);
		zip.writeZip(destArchivePath);
		return;
	}
	if (t === '.TAR' || t === '.TAR.GZ' || t === '.TGZ') {
		const gzip = t === '.TAR.GZ' || t === '.TGZ';
		const files = await listFilesRecursive(srcDir);
		await tar.c({ cwd: srcDir, file: destArchivePath, gzip }, files);
		return;
	}
	if (t === '.7Z') {
		const cmd = await sevenZipCommand();
		// Create 7z archive from directory contents.
		await spawnCommand(cmd, ['a', '-t7z', '-y', '-mx=7', destArchivePath, '.'], { cwd: srcDir });
		return;
	}
	throw new Error(`Unsupported archive target: ${targetExt}`);
}

async function convertArchive(srcPath: string, destArchivePath: string, targetExt: string): Promise<void> {
	const tmpDir = await fs.mkdtemp(path.join(os.tmpdir(), 'convertable-archive-'));
	try {
		await extractArchive(srcPath, tmpDir);
		await createArchiveFromDir(tmpDir, destArchivePath, targetExt);
	} finally {
		try {
			await fs.rm(tmpDir, { recursive: true, force: true });
		} catch {
			// ignore
		}
	}
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

async function convertImageToPdf(srcPath: string, destPath: string): Promise<void> {
	// pdf-lib supports embedding PNG/JPEG. We normalize via sharp -> PNG.
	const pngBytes = await sharp(srcPath).png().toBuffer();
	const pdfDoc = await PDFDocument.create();
	const embedded = await pdfDoc.embedPng(pngBytes);
	const page = pdfDoc.addPage([embedded.width, embedded.height]);
	page.drawImage(embedded, { x: 0, y: 0, width: embedded.width, height: embedded.height });
	const pdfBytes = await pdfDoc.save();
	await fs.writeFile(destPath, pdfBytes);
}

async function spawnCommand(cmd: string, args: string[], opts?: { cwd?: string }): Promise<void> {
	return new Promise((resolve, reject) => {
		const child = spawn(cmd, args, { cwd: opts?.cwd, stdio: ['ignore', 'ignore', 'pipe'] });
		let stderr = '';
		child.stderr.on('data', (buf) => {
			stderr += buf.toString('utf-8');
		});
		child.on('error', (err) => {
			reject(err);
		});
		child.on('close', (code) => {
			if (code === 0) resolve();
			reject(new Error(stderr || `${cmd} exited with code ${code}`));
		});
	});
}

async function spawnCommandCapture(cmd: string, args: string[], opts?: { cwd?: string }): Promise<{ stdout: string; stderr: string }> {
	return new Promise((resolve, reject) => {
		const child = spawn(cmd, args, { cwd: opts?.cwd, stdio: ['ignore', 'pipe', 'pipe'] });
		let stdout = '';
		let stderr = '';
		child.stdout.on('data', (buf) => {
			stdout += buf.toString('utf-8');
		});
		child.stderr.on('data', (buf) => {
			stderr += buf.toString('utf-8');
		});
		child.on('error', (err) => reject(err));
		child.on('close', (code) => {
			if (code === 0) {
				resolve({ stdout, stderr });
				return;
			}
			reject(new Error(stderr || `${cmd} exited with code ${code}`));
		});
	});
}

function looksLikeSharpPdfUnsupported(err: unknown): boolean {
	const msg = err instanceof Error ? err.message : String(err);
	const m = msg.toLowerCase();
	return (
		m.includes('unsupported image format') ||
		(m.includes('unsupported') && m.includes('pdf')) ||
		m.includes('no decode delegate') ||
		(m.includes('vips') && m.includes('pdf'))
	);
}

async function convertPdfToImagesViaSips(
	srcPath: string,
	outDir: string,
	targetExt: string,
): Promise<{ outputPath: string; outputExt: string }> {
	// macOS fallback: `sips` can rasterize PDFs (typically first page only).
	const t = targetExt.toUpperCase();
	const outExt = t === '.JPG' ? '.JPEG' : t;

	if (outExt === '.WEBP') {
		// sips can't emit webp; do PDF->PNG via sips, then PNG->WEBP via sharp.
		const tmpDir = await fs.mkdtemp(path.join(os.tmpdir(), 'convertable-pdf-'));
		try {
			const tmpPng = path.join(tmpDir, `${path.parse(srcPath).name}.png`);
			await spawnCommand('sips', ['-s', 'format', 'png', srcPath, '--out', tmpPng]);
			const outputPath = await buildNonCollidingOutputPath(srcPath, '.webp', outDir);
			await sharp(tmpPng).webp({ quality: 90 }).toFile(outputPath);
			return { outputPath, outputExt: outExt };
		} finally {
			try {
				await fs.rm(tmpDir, { recursive: true, force: true });
			} catch {
				// ignore
			}
		}
	}

	const sipsFormat = outExt === '.PNG' ? 'png' : 'jpeg';
	const outFileExtLower = outExt === '.PNG' ? '.png' : '.jpg';
	const outputPath = await buildNonCollidingOutputPath(srcPath, outFileExtLower, outDir);
	await spawnCommand('sips', ['-s', 'format', sipsFormat, srcPath, '--out', outputPath]);
	return { outputPath, outputExt: outExt };
}

async function convertPdfToImages(
	srcPath: string,
	outDir: string,
	targetExt: string,
): Promise<{ outputPath: string; outputExt: string }> {
	// sharp can rasterize PDFs (first or all pages) when built with PDF support.
	const density = 200;
	const t = targetExt.toUpperCase();
	const outExt = t === '.JPG' ? '.JPEG' : t;

	let pageCount = 1;
	try {
		const meta = await sharp(srcPath, { density }).metadata();
		pageCount = typeof meta.pages === 'number' && meta.pages > 0 ? meta.pages : 1;
	} catch (err) {
		if (process.platform === 'darwin') {
			return convertPdfToImagesViaSips(srcPath, outDir, outExt);
		}
		if (looksLikeSharpPdfUnsupported(err)) {
			throw new Error(
				'PDF rasterization is not available in this build of sharp/libvips. Install a sharp build with PDF support (poppler/pdfium) or use a different backend.'
			);
		}
		throw err;
	}

	const baseName = path.parse(srcPath).name;
	const imageExtLower = outExt.toLowerCase() === '.jpeg' ? '.jpg' : outExt.toLowerCase();

	// Single page: write a single image.
	if (pageCount <= 1) {
		const outputPath = await buildNonCollidingOutputPath(srcPath, imageExtLower, outDir);
		const img = sharp(srcPath, { density, page: 0 });
		if (outExt === '.PNG') {
			await img.png().toFile(outputPath);
			return { outputPath, outputExt: outExt };
		}
		if (outExt === '.WEBP') {
			await img.webp({ quality: 90 }).toFile(outputPath);
			return { outputPath, outputExt: outExt };
		}
		await img.jpeg({ quality: 92 }).toFile(outputPath);
		return { outputPath, outputExt: outExt };
	}

	// Multi-page: package rendered pages into a ZIP so the UI can treat it as one output.
	const zipPath = await buildNonCollidingOutputPath(srcPath, '.zip', outDir);
	const zip = new JSZip();

	for (let i = 0; i < pageCount; i += 1) {
		const img = sharp(srcPath, { density, page: i });
		let buf: Buffer;
		let extInZip: string;
		if (outExt === '.PNG') {
			buf = await img.png().toBuffer();
			extInZip = 'png';
		} else if (outExt === '.WEBP') {
			buf = await img.webp({ quality: 90 }).toBuffer();
			extInZip = 'webp';
		} else {
			buf = await img.jpeg({ quality: 92 }).toBuffer();
			extInZip = 'jpg';
		}
		const name = `${baseName}_page_${String(i + 1).padStart(3, '0')}.${extInZip}`;
		zip.file(name, buf);
	}

	const zipBytes = await zip.generateAsync({ type: 'nodebuffer', compression: 'DEFLATE' });
	await fs.writeFile(zipPath, zipBytes);
	return { outputPath: zipPath, outputExt: '.ZIP' };
}

async function spawnFfmpeg(args: string[]): Promise<void> {
	const cmd = await ffmpegCommand();
	return spawnFfmpegWithProgress(args);
}

function parseHmsTimestamp(value: string): number | null {
	// Expected: HH:MM:SS[.ms]
	const m = /^\s*(\d+):(\d+):(\d+(?:\.\d+)?)\s*$/.exec(value);
	if (!m) return null;
	const h = Number(m[1]);
	const min = Number(m[2]);
	const s = Number(m[3]);
	if (!Number.isFinite(h) || !Number.isFinite(min) || !Number.isFinite(s)) return null;
	return h * 3600 + min * 60 + s;
}

async function spawnFfmpegWithProgress(
	args: string[],
	onProgress?: (progress: number) => void,
): Promise<void> {
	const cmd = await ffmpegCommand();
	return new Promise((resolve, reject) => {
		const child = spawn(cmd, args, { stdio: ['ignore', 'ignore', 'pipe'] });

		// Keep only a tail of stderr for errors so we don't grow unbounded.
		let stderrTail = '';
		let bufRemainder = '';
		let durationSec: number | null = null;
		let lastProgress = 0;
		let lastProgressUpdateAt = Date.now();
		let idleTimer: NodeJS.Timeout | null = null;

		function pushStderrTail(text: string) {
			stderrTail += text;
			const max = 32_768;
			if (stderrTail.length > max) stderrTail = stderrTail.slice(stderrTail.length - max);
		}

		function handleLine(line: string) {
			// Example: Duration: 00:03:12.34, start: 0.000000, bitrate: ...
			if (durationSec == null) {
				const dm = /Duration:\s*(\d+:\d+:\d+(?:\.\d+)?)/.exec(line);
				if (dm) {
					const d = parseHmsTimestamp(dm[1] ?? '');
					if (d != null && d > 0) durationSec = d;
				}
			}

			// Example: ... time=00:00:05.12 ...
			if (durationSec != null && durationSec > 0 && onProgress) {
				const tm = /time=(\d+:\d+:\d+(?:\.\d+)?)/.exec(line);
				if (tm) {
					const t = parseHmsTimestamp(tm[1] ?? '');
					if (t != null && t >= 0) {
						const p = Math.max(0, Math.min(0.999, t / durationSec));
						// Avoid noisy updates and regressions.
						if (p > lastProgress + 0.002) {
							lastProgress = p;
							lastProgressUpdateAt = Date.now();
							onProgress(p);
						}
					}
				}
			}
		}

		if (onProgress) {
			// Some ffmpeg operations (especially container finalization/muxing) can
			// pause the `time=` counter for a while even though work continues.
			// Gently advance progress so the UI doesn't look frozen, but never hit 100%
			// until the process exits successfully.
			idleTimer = setInterval(() => {
				if (durationSec == null || durationSec <= 0) return;
				const idleMs = Date.now() - lastProgressUpdateAt;
				if (idleMs < 6_000) return;
				if (lastProgress >= 0.99) return;
				// Advance slowly: 0.5% per tick after being idle.
				lastProgress = Math.min(0.99, lastProgress + 0.005);
				lastProgressUpdateAt = Date.now();
				try {
					onProgress(lastProgress);
				} catch {
					// ignore
				}
			}, 1000);
		}

		child.stderr.on('data', (buf) => {
			const text = buf.toString('utf-8');
			pushStderrTail(text);
			bufRemainder += text;
			// ffmpeg frequently uses carriage returns for its status line.
			const parts = bufRemainder.split(/\r\n|\n|\r/g);
			bufRemainder = parts.pop() ?? '';
			for (const p of parts) handleLine(p);
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
			if (idleTimer) {
				clearInterval(idleTimer);
				idleTimer = null;
			}
			if (code === 0) {
				resolve();
				return;
			}
			reject(new Error(stderrTail || `ffmpeg exited with code ${code}`));
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

let cached7zCommand: string | null = null;
async function sevenZipCommand(): Promise<string> {
	if (cached7zCommand) return cached7zCommand;
	try {
		const mod = (await import('7zip-bin')) as unknown as SevenZipBinModule;
		const p: unknown = mod.path7za ?? mod.default?.path7za;
		if (typeof p === 'string' && p) {
			cached7zCommand = p;
			return cached7zCommand;
		}
	} catch {
		// ignore
	}
	cached7zCommand = '7za';
	return cached7zCommand;
}

async function convertWithFfmpeg(
	srcPath: string,
	destPath: string,
	targetExt: string,
	onProgress?: (progress: number) => void,
): Promise<void> {
	const t = targetExt.toUpperCase();
	const args: string[] = ['-y', '-i', srcPath];
	if (t === '.MP3') {
		args.push('-vn', '-c:a', 'libmp3lame', '-q:a', '2', destPath);
		await spawnFfmpegWithProgress(args, onProgress);
		return;
	}
	if (t === '.WAV') {
		args.push('-vn', '-c:a', 'pcm_s16le', destPath);
		await spawnFfmpegWithProgress(args, onProgress);
		return;
	}
	if (t === '.M4A') {
		args.push('-vn', '-c:a', 'aac', '-b:a', '192k', destPath);
		await spawnFfmpegWithProgress(args, onProgress);
		return;
	}
	if (t === '.MP4') {
		args.push('-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '192k', destPath);
		await spawnFfmpegWithProgress(args, onProgress);
		return;
	}
	// .MOV (simple h264-in-mov)
	args.push('-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '192k', destPath);
	await spawnFfmpegWithProgress(args, onProgress);
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
		let timer: NodeJS.Timeout | null = setInterval(() => {
			const elapsed = Date.now() - startedAt;
			// Ease-out curve that approaches 0.95 asymptotically.
			const p = Math.min(0.95, 0.95 * (1 - Math.exp(-elapsed / 1200)));
			if (p > lastEmitted + 0.005) {
				lastEmitted = p;
				this.emit({ type: 'progress', srcPath, targetExt, progress: p });
			}
		}, tickMs);

		const stopPseudo = () => {
			if (timer) {
				clearInterval(timer);
				timer = null;
			}
		};

		const { effective: outDirResolved } = await resolveEffectiveOutputDir();
		// Some conversions (PDF multi-page) may output a different extension (ZIP).
		let outputPath = withNewExtension(srcPath, targetExt, outDirResolved);

		try {
			const srcMime = detectMime(srcPath);
			const srcExt = normalizedExt(srcPath).toUpperCase();
			const t = targetExt.toUpperCase();

			if (t === '.PDF') {
				// Currently: only support image -> PDF.
				if (srcMime === 'application/pdf') {
					throw new Error('Converting PDF to PDF is not supported.');
				}
				outputPath = await buildNonCollidingOutputPath(srcPath, '.pdf', outDirResolved);
				await convertImageToPdf(srcPath, outputPath);
			} else if (srcMime === 'application/pdf' && isImageTarget(t)) {
				const res = await convertPdfToImages(srcPath, outDirResolved, t);
				outputPath = res.outputPath;
			} else if (isImageTarget(t)) {
				await convertImage(srcPath, outputPath, t);
			} else if (isMediaTarget(t)) {
				await convertWithFfmpeg(srcPath, outputPath, t, (p) => {
					// Switch to real ffmpeg progress once we have it.
					stopPseudo();
					if (p > lastEmitted + 0.002) {
						lastEmitted = p;
						this.emit({ type: 'progress', srcPath, targetExt, progress: p });
					}
				});
			} else if (isExtractTarget(t)) {
				if (!isArchiveSourceExt(srcExt)) {
					throw new Error('Extract is only supported for archive inputs (.zip, .tar, .tar.gz/.tgz, .rar).');
				}
				outputPath = await buildNonCollidingOutputDir(srcPath, ' (extracted)', outDirResolved);
				await extractArchive(srcPath, outputPath);
			} else if (isArchiveTarget(t)) {
				if (!isArchiveSourceExt(srcExt)) {
					throw new Error('Archive conversion is only supported for archive inputs (.zip, .tar, .tar.gz/.tgz, .rar).');
				}
				outputPath = await buildNonCollidingOutputPath(srcPath, t, outDirResolved);
				await convertArchive(srcPath, outputPath, t);
			} else {
				throw new Error(`Unsupported target: ${targetExt}`);
			}
			stopPseudo();
			this.emit({ type: 'progress', srcPath, targetExt, progress: 1 });
			this.emit({ type: 'done', srcPath, outputPath, targetExt });
		} catch (err) {
			stopPseudo();
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
	// Initialize settings after Electron is ready.
	getSettingsStore();

	ipcMain.on('files/startDrag', (event, args: { path: string }) => {
		const p = typeof args?.path === 'string' ? args.path : '';
		if (!p) return;
		let icon = nativeImage.createEmpty();
		try {
			// If the file is an image, this gives a nice drag preview.
			const candidate = nativeImage.createFromPath(p);
			if (!candidate.isEmpty()) {
				// macOS can render very large drag previews if the icon is huge.
				// Keep it to a small, consistent size while preserving aspect ratio.
				const { width, height } = candidate.getSize();
				const maxDim = 128;
				if (width > 0 && height > 0) {
					icon = width >= height
						? candidate.resize({ width: maxDim, quality: 'good' })
						: candidate.resize({ height: maxDim, quality: 'good' });
				} else {
					icon = candidate.resize({ width: maxDim, quality: 'good' });
				}
			}
		} catch {
			// ignore
		}
		if (icon.isEmpty()) {
			// Ensure we always provide a valid icon so drag-out works for non-images.
			const transparentPng =
				'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4nGNgYAAAAAMAASsJTYQAAAAASUVORK5CYII=';
			try {
				icon = nativeImage.createFromDataURL(transparentPng);
			} catch {
				// ignore
			}
		}
		try {
				// Prefer the modern API; this also handles directories reliably.
				event.sender.startDrag({ files: [p], icon } as any);
		} catch {
				try {
					// Back-compat for older Electron versions.
					event.sender.startDrag({ file: p, icon } as any);
				} catch {
					// ignore
				}
		}
	});

	ipcMain.handle('files/pick', async () => {
		const res = await dialog.showOpenDialog({
			properties: ['openFile', 'multiSelections'],
			filters: [
				{
					name: 'Supported',
					extensions: [
						'png',
						'jpg',
						'jpeg',
						'webp',
						'mp3',
						'wav',
						'm4a',
						'mp4',
						'mov',
						'pdf',
						'zip',
						'rar',
						'7z',
						'tar',
						'gz',
						'tgz',
					],
				},
				{ name: 'All Files', extensions: ['*'] },
			],
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

	ipcMain.handle('settings/getOutputDir', async () => {
		const { configured, effective } = await resolveEffectiveOutputDir();
		return { configured, effective };
	});

	ipcMain.handle('settings/resetOutputDir', async () => {
		await setConfiguredOutputDir(null);
		const { configured, effective } = await resolveEffectiveOutputDir();
		return { configured, effective };
	});

	ipcMain.handle('settings/pickOutputDir', async () => {
		const res = await dialog.showOpenDialog({
			properties: ['openDirectory', 'createDirectory'],
		});
		if (res.canceled || res.filePaths.length === 0) {
			const { configured, effective } = await resolveEffectiveOutputDir();
			return { configured, effective, canceled: true };
		}
		const picked = res.filePaths[0] ?? '';
		const configured = await setConfiguredOutputDir(picked);
		const { effective } = await resolveEffectiveOutputDir();
		return { configured, effective, canceled: false };
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
