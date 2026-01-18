// CommonJS preload: Electron reliably loads CJS preload scripts.
// This keeps window.convertable available even when the app uses ESM elsewhere.

const { contextBridge, ipcRenderer, webUtils } = require('electron');

const api = {
	getPathsForFiles(files) {
		if (!Array.isArray(files)) return [];
		const out = [];
		for (const f of files) {
			try {
				if (webUtils && typeof webUtils.getPathForFile === 'function') {
					const p = webUtils.getPathForFile(f);
					if (typeof p === 'string' && p) out.push(p);
					continue;
				}
				// Fallback for older Electron / non-standard File objects.
				const p = f && typeof f.path === 'string' ? f.path : '';
				if (p) out.push(p);
			} catch {
				// ignore
			}
		}
		return out;
	},

	pickFiles() {
		return ipcRenderer.invoke('files/pick');
	},

	getFileMetadata(paths) {
		return ipcRenderer.invoke('files/metadata', { paths });
	},

	revealInFinder(path) {
		return ipcRenderer.invoke('files/reveal', { path });
	},

	startDrag(path) {
		ipcRenderer.send('files/startDrag', { path });
	},

	enqueueJobs(jobs) {
		return ipcRenderer.invoke('engine/enqueue', { jobs });
	},

	onEngineEvent(handler) {
		const listener = (_event, ev) => {
			try {
				handler(ev);
			} catch {
				// ignore handler errors
			}
		};
		ipcRenderer.on('engine/event', listener);
		return () => {
			ipcRenderer.removeListener('engine/event', listener);
		};
	},
};

contextBridge.exposeInMainWorld('convertable', api);
