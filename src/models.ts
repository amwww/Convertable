export interface DroppedFile {
	path: string;
	name: string;
	sizeBytes: number | null;
	mime: string;
	ext: string;
}

export interface ConversionJob {
	sourcePath: string;
	sourceName: string;
	targetExt: string;
	workerId?: number;
	status: string;
	progress?: number;
	error?: string;
}

export interface ConversionResultItem {
	sourcePath: string;
	sourceName: string;
	outputPath: string;
	targetExt: string;
}

export type EngineEvent =
| { type: 'start'; workerId: number; srcPath: string; targetExt: string }
	| { type: 'progress'; workerId: number; srcPath: string; targetExt: string; progress: number }
| { type: 'done'; workerId: number; srcPath: string; outputPath: string; targetExt: string }
	| { type: 'canceled'; workerId: number; srcPath: string; targetExt: string }
	| { type: 'error'; workerId: number; srcPath: string; targetExt: string; message: string };
