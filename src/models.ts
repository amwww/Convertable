export interface DroppedFile {
	path: string;
	name: string;
	sizeBytes: number | null;
	mime: string;
	ext: string;
	mtimeMs?: number;
	width?: number;
	height?: number;
	addedAtMs?: number;
}

export interface ConversionJob {
	sourcePath: string;
	sourceName: string;
	targetExt: string;
	workerId?: number;
	scale?: number;
	status: string;
	progress?: number;
	error?: string;
}

export interface ConversionResultItem {
	sourcePath: string;
	sourceName: string;
	outputPath: string;
	targetExt: string;
	scale?: number;
	durationMs?: number;
	outputWidth?: number;
	outputHeight?: number;
	status?: 'done' | 'error';
	error?: string;
	command?: string;
}

export type EngineEvent =
	| { type: 'start'; workerId: number; srcPath: string; targetExt: string; scale?: number }
	| { type: 'progress'; workerId: number; srcPath: string; targetExt: string; progress: number; scale?: number }
	| { type: 'done'; workerId: number; srcPath: string; outputPath: string; targetExt: string; scale?: number; outputWidth?: number; outputHeight?: number; command?: string }
	| { type: 'canceled'; workerId: number; srcPath: string; targetExt: string; scale?: number }
	| { type: 'error'; workerId: number; srcPath: string; targetExt: string; message: string; scale?: number; command?: string };
