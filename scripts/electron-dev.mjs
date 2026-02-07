import { spawn } from 'node:child_process';
import fs from 'node:fs/promises';
import path from 'node:path';

function hasFlag(name) {
	return process.argv.includes(name);
}

function binPath(name) {
	const ext = process.platform === 'win32' ? '.cmd' : '';
	return path.resolve('node_modules', '.bin', `${name}${ext}`);
}

async function exists(p) {
	try {
		await fs.access(p);
		return true;
	} catch {
		return false;
	}
}

async function waitForFiles(files, { timeoutMs = 60_000, pollMs = 200 } = {}) {
	const started = Date.now();
	while (Date.now() - started < timeoutMs) {
		const checks = await Promise.all(files.map((f) => exists(f)));
		if (checks.every(Boolean)) return;
		await new Promise((r) => setTimeout(r, pollMs));
	}
	throw new Error(`Timed out waiting for build outputs: ${files.join(', ')}`);
}

const noWatch = hasFlag('--no-watch') || hasFlag('--noWatch') || process.env.CONVERTABLE_NO_WATCH === '1';

const required = [
	path.resolve('dist', 'main.js'),
	path.resolve('dist', 'preload.js'),
	path.resolve('dist', 'index.js'),
];

await waitForFiles(required);

const electronBin = binPath('electron');

if (noWatch) {
	const child = spawn(electronBin, ['.'], { stdio: 'inherit' });
	child.on('exit', (code) => process.exit(code ?? 0));
} else {
	const nodemonBin = binPath('nodemon');
	const args = [
		'--delay',
		'1500ms',
		'--signal',
		'SIGTERM',
		'--watch',
		'dist',
		'--watch',
		'index.html',
		'--ext',
		'js,html',
		'--exec',
		electronBin,
		'.',
	];
	const child = spawn(nodemonBin, args, { stdio: 'inherit' });
	child.on('exit', (code) => process.exit(code ?? 0));
}
