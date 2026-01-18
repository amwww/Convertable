import { contextBridge, ipcRenderer, webUtils } from 'electron';

import type { EngineEvent } from './models.js';

type DroppedFile = {
	path: string;
	name: string;
	sizeBytes: number | null;
	mime: string;
	ext: string;
};

type EnqueueJob = { srcPath: string; targetExt: string };

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

	enqueueJobs(jobs: EnqueueJob[]): Promise<void> {
		return ipcRenderer.invoke('engine/enqueue', { jobs });
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
