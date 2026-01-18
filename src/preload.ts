import { contextBridge, ipcRenderer } from 'electron';

import type { EngineEvent } from './models.js';

type EnqueueJob = { srcPath: string; targetExt: string };

const api = {
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
