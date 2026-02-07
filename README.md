# Convertable

Convertable is a small file conversion app.

This repo currently contains an **Electron** implementation (TypeScript) and a **legacy Tk/PyInstaller** implementation under `tktinker/`.

## Quick start (Electron)

```bash
npm install
npm run dev
```

## Archive support

The Electron build supports:

- Extract: `.zip`, `.tar`, `.tar.gz` / `.tgz`, `.rar`, `.7z`
- Convert archives to: `.zip`, `.tar`, `.tar.gz` / `.tgz`, `.7z`

Notes:

- `.rar` extraction uses an in-memory WASM extractor; very large RARs may be rejected to avoid running out of memory.
- `.7z` support uses a bundled `7za` binary via `7zip-bin`.

## Packaging (macOS)

See `BUILDING.md` for how to produce a `.app` bundle.

## Legacy app

See `tktinker/BUILDING.md` for the PyInstaller build.
