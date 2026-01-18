import { app, BrowserWindow, ipcMain } from 'electron';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import type { EngineEvent } from './models.js';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

type EnqueueJob = { srcPath: string; targetExt: string };

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

    this.emit({ type: 'start', srcPath, targetExt });

    const startTime = Date.now();
    const durationMs = 1500;

    const tick = () => {
      const now = Date.now();
      const progress = Math.min(1, (now - startTime) / durationMs);
      this.emit({ type: 'progress', srcPath, progress });
      if (progress >= 1) {
        const outputPath = `${srcPath}.converted.${targetExt.replace(/^\./, '')}`;
        this.emit({ type: 'done', srcPath, outputPath, targetExt });
        this.processing = false;
        this.maybeStartNext();
      } else {
        setTimeout(tick, 150);
      }
    };

    setTimeout(tick, 150);
  }
}

const engine = new ConversionEngine((event: EngineEvent) => {
  for (const win of BrowserWindow.getAllWindows()) {
    win.webContents.send('engine/event', event);
  }
});

function createWindow() {
  const win = new BrowserWindow({
    width: 800,
    height: 600,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
    },
  });

  // index.html lives one level up from dist/
  const htmlPath = path.join(__dirname, '../index.html');
  win.loadFile(htmlPath);

  win.webContents.on('did-finish-load', () => {
    console.log('Renderer loaded:', htmlPath);
  });

  win.webContents.on('did-fail-load', (_event, errorCode, errorDescription) => {
    console.error('Renderer failed to load:', htmlPath, errorCode, errorDescription);
  });
}

app.whenReady().then(() => {
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
