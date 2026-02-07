# Building Convertable (Electron)

This repo contains two apps:

- **Electron app (current)**: TypeScript + Electron (this folder)
- **Legacy Tk/PyInstaller app**: see `tktinker/` and `tktinker/BUILDING.md`

- Node.js + npm
- macOS (for building a `.app` bundle)

## Dev

npm install
npm run dev
```
- TypeScript compiles in watch mode.
- Electron restarts automatically when `dist/` changes.

## Run (one-shot)

```bash
npm install
npm run start
```

## Package a macOS `.app`

### Build an unsigned `.app` (recommended for local use)

```bash
npm run pack:mac
```

Output:

- `release/mac-arm64/Convertable.app` (Apple Silicon)

### Build “dist” artifacts

```bash
npm run dist:mac
```

This runs electron-builder’s mac pipeline. If you have signing configured, it can also sign/notarize.

## Signing / notarization notes

If you see “skipped macOS application code signing”, that’s expected unless you have a valid **Developer ID Application** certificate.

If you have old/expired certificates in your keychain, electron-builder may log them while auto-discovering identities. This repo configures unsigned local builds by default (see the `build.mac.identity` setting in `package.json`).

- Unsigned builds usually run on your machine.
- To distribute to other Macs without scary Gatekeeper prompts, you typically need:
  - Developer ID Application signing
  - Notarization + stapling

Electron-builder supports this, but it requires Apple developer credentials and is not enabled by default here.

## PDF conversion notes (Electron)

- **PDF → images**: implemented in the Electron main process.
  - Uses `sharp` when PDF decoding is available.
  - On macOS, falls back to `sips` if sharp/libvips can’t decode PDFs.
- **Image → PDF**: uses `pdf-lib`.
- Multi-page PDFs produce a single `.zip` containing one image per page.

## Troubleshooting

### Two Electron windows during `npm run dev`

If Electron appears twice during rebuilds, it’s usually nodemon restarting quickly while `tsc -w` is emitting multiple files.

This repo uses:

- `nodemon --delay 1500ms --signal SIGTERM`
- SIGTERM handling in `src/main.ts`

…to ensure the old Electron instance exits cleanly before restart.
