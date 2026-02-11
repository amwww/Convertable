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

## Create a macOS `.dmg` installer

This project uses **electron-builder** to generate `.dmg` artifacts.

```bash
npm run dist:mac
```

Output (under `release/`):

- `Convertable-<version>-arm64.dmg` (Apple Silicon)
- `Convertable-<version>-arm64.zip`

Note:

- `npm run pack:mac` is still the easiest way to get a local `.app` bundle.
- `.dmg`/`.zip` names include the version from `package.json`.

### Build Intel (x64)

```bash
npm run dist:mac:x64
```

Output (under `release/`):

- `Convertable-<version>-x64.dmg` (Intel)
- `Convertable-<version>-x64.zip`

### Build both Apple Silicon + Intel

```bash
npm run dist:mac:all
```

Output (under `release/`):

- `Convertable-<version>-arm64.dmg`
- `Convertable-<version>-arm64.zip`
- `Convertable-<version>-x64.dmg`
- `Convertable-<version>-x64.zip`

## Windows builds

Windows builds are easiest on CI (Windows runner), but you can try locally if you have the required tooling.

```bash
npm run dist:win
```

Expected output (under `release/`):

- `Convertable-<version>-x64.exe`

## Linux builds

Linux builds are easiest on CI (Ubuntu runner).

```bash
npm run dist:linux
```

Expected output (under `release/`):

- `Convertable-<version>-x64.AppImage`
- `Convertable-<version>-x64.deb`

## In-app auto-updates (GitHub Releases)

This app supports in-app updates via **electron-updater**.

### What to upload to GitHub Releases

For macOS auto-updates, upload these files from `release/`:

- `latest-mac.yml`
- `Convertable-<version>-arm64.zip`
- `Convertable-<version>-arm64.zip.blockmap`
- `Convertable-<version>-x64.zip`
- `Convertable-<version>-x64.zip.blockmap`

Recommended for first-time installs (website downloads):

- `Convertable-<version>-arm64.dmg`
- `Convertable-<version>-x64.dmg`

Note: the `.dmg` is for installers; auto-update typically uses the `.zip` + `.blockmap` files.

### Important macOS signing note

For seamless auto-update *installation* to work reliably on other Macs, the app usually must be **code signed** (Developer ID) and **notarized**.

If you don’t have an Apple Developer account, expect macOS Gatekeeper warnings. This repo is configured to still support
in-app **update checks**, but updates are applied via **manual download** (the app opens GitHub Releases).

## “Convertable is damaged and can’t be opened” (macOS)

This message is usually Gatekeeper blocking an app that is **not signed/notarized** (or has an invalid signature).

### Temporary workaround (local testing)

If you downloaded the app/DMG from the internet, macOS applies a quarantine attribute. You can remove it on the installed app:

```bash
xattr -dr com.apple.quarantine "/Applications/Convertable.app"
```

Then try opening again.

### Proper fix (for real users + auto-updates)

For distribution (and for true in-app auto-install updates), build **signed + notarized** artifacts:

- Install a **Developer ID Application** certificate in your keychain.
- Set environment variables:
  - `APPLE_ID`
  - `APPLE_APP_SPECIFIC_PASSWORD`
  - `APPLE_TEAM_ID`

This repo includes a notarization hook at [scripts/notarize.mjs](scripts/notarize.mjs). When those env vars are set,
electron-builder will notarize during `dist:mac` runs.

If you want unsigned builds for local use, use:

```bash
npm run dist:mac:unsigned
```

## Recommended release flow (GitHub Actions)

This repo includes a multi-platform release workflow at [.github/workflows/release.yml](.github/workflows/release.yml).

To create a release build for macOS + Windows + Linux:

1) Bump version in `package.json`
2) Create and push a tag like `v1.0.1`

GitHub Actions will build on macOS/Windows/Linux and upload artifacts to a GitHub Release for that tag.

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
