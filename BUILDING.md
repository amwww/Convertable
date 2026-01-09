# Building Convertable (macOS) with PyInstaller

## 1) Install build deps

From the project folder:

```bash
python -m pip install -r requirements.txt
python -m pip install pyinstaller
```

## 2) (Optional) Add an app icon

PyInstaller on macOS expects an `.icns` icon for the Dock / app switcher.

- Put an icon at `assets/icon.icns` (recommended), or `icon.icns`.

If you currently have a PNG (e.g. `assets/icon.png`), you can create an `.icns` like this:

```bash
mkdir -p assets/icon.iconset
# Generate a set of sizes (adjust source path if needed)
sips -z 16 16     assets/icon.png --out assets/icon.iconset/icon_16x16.png
sips -z 32 32     assets/icon.png --out assets/icon.iconset/icon_16x16@2x.png
sips -z 32 32     assets/icon.png --out assets/icon.iconset/icon_32x32.png
sips -z 64 64     assets/icon.png --out assets/icon.iconset/icon_32x32@2x.png
sips -z 128 128   assets/icon.png --out assets/icon.iconset/icon_128x128.png
sips -z 256 256   assets/icon.png --out assets/icon.iconset/icon_128x128@2x.png
sips -z 256 256   assets/icon.png --out assets/icon.iconset/icon_256x256.png
sips -z 512 512   assets/icon.png --out assets/icon.iconset/icon_256x256@2x.png
sips -z 512 512   assets/icon.png --out assets/icon.iconset/icon_512x512.png
sips -z 1024 1024 assets/icon.png --out assets/icon.iconset/icon_512x512@2x.png
iconutil -c icns assets/icon.iconset -o assets/icon.icns
```

## 3) Build the `.app`

Use the checked-in spec file:

```bash
pyinstaller --noconfirm --clean convertable.spec
```

Your app will be at:

- `dist/Convertable.app`

## Notes

- When bundled, macOS will show the app name as **Convertable** (not **Python**).
- If you don’t provide an `.icns`, the app may build but will use a default icon.
