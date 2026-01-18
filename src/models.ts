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
  status: string;
}

export interface ConversionResultItem {
  sourcePath: string;
  sourceName: string;
  outputPath: string;
  targetExt: string;
}

export type EngineEvent =
  | { type: 'start'; srcPath: string; targetExt: string }
  | { type: 'progress'; srcPath: string; progress: number }
  | { type: 'done'; srcPath: string; outputPath: string; targetExt: string }
  | { type: 'error'; srcPath: string; message: string };
