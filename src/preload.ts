import { contextBridge, ipcRenderer, webUtils } from 'electron';

import type { EngineEvent } from './models.js';

type DroppedFile = {
	path: string;
	name: string;
	sizeBytes: number | null;
	mime: string;
	ext: string;
};

type EnqueueJob = { srcPath: string; targetExt: string; workerId?: number };

type OutputDirInfo = { configured: string | null; effective: string };

const api = {
	getPathsForFiles(files: File[]): string[] {
		if (!Array.isArray(files)) return [];
		const out: string[] = [];
		for (const f of files) {
			try {
				const p = webUtils.getPathForFile(f);
				if (typeof p === 'string' && p) out.push(p);
			} catch {
				// ignore
			}
		}
		return out;
	},

	pickFiles(): Promise<DroppedFile[]> {
		return ipcRenderer.invoke('files/pick');
	},

	getFileMetadata(paths: string[]): Promise<DroppedFile[]> {
		return ipcRenderer.invoke('files/metadata', { paths });
	},

	revealInFinder(path: string): Promise<void> {
		return ipcRenderer.invoke('files/reveal', { path });
	},

	startDrag(path: string): void {
		ipcRenderer.send('files/startDrag', { path });
	},

	getOutputDir(): Promise<OutputDirInfo> {
		return ipcRenderer.invoke('settings/getOutputDir');
	},

	pickOutputDir(): Promise<OutputDirInfo & { canceled: boolean }> {
		return ipcRenderer.invoke('settings/pickOutputDir');
	},

	resetOutputDir(): Promise<OutputDirInfo> {
		return ipcRenderer.invoke('settings/resetOutputDir');
	},

	getCpuThreads(): Promise<number | null> {
		return ipcRenderer.invoke('settings/getCpuThreads');
	},

	setCpuThreads(threads: number | null): Promise<number | null> {
		return ipcRenderer.invoke('settings/setCpuThreads', { threads });
	},

	copyToClipboard(text: string): Promise<void> {
		return ipcRenderer.invoke('sys/copyText', { text });
	},

	cancelCurrent(): Promise<{ canceled: boolean }> {
		return ipcRenderer.invoke('engine/cancelCurrent');
	},

	cancelWorker(workerId: number): Promise<{ canceled: boolean }> {
		return ipcRenderer.invoke('engine/cancelWorker', { workerId });
	},

	cancelAll(): Promise<{ canceledCurrent: boolean; canceledPending: number }> {
		return ipcRenderer.invoke('engine/cancelAll');
	},

	enqueueJobs(jobs: EnqueueJob[]): Promise<void> {
		return ipcRenderer.invoke('engine/enqueue', { jobs });
	},

	getWorkerCount(): Promise<number> {
		return ipcRenderer.invoke('engine/getWorkerCount');
	},

	setWorkerCount(count: number): Promise<number> {
		return ipcRenderer.invoke('engine/setWorkerCount', { count });
	},

	getPaused(): Promise<boolean> {
		return ipcRenderer.invoke('engine/getPaused');
	},

	setPaused(paused: boolean): Promise<boolean> {
		return ipcRenderer.invoke('engine/setPaused', { paused });
	},

	setPendingQueues(queues: EnqueueJob[][]): Promise<void> {
		return ipcRenderer.invoke('engine/setPendingQueues', { queues });
	},
	
	onEngineEvent(handler: (event: EngineEvent) => void): () => void {
		const listener = (_event: Electron.IpcRendererEvent, ev: EngineEvent) => {
			handler(ev);
		};
		ipcRenderer.on('engine/event', listener);
		return () => {
			ipcRenderer.removeListener('engine/event', listener);
		};
	},
};

contextBridge.exposeInMainWorld('convertable', api);
