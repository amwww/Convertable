# Convertable website

This folder is a standalone static marketing website for Convertable.

It is intentionally separate from the Electron app’s UI `index.html` at the repo root.

## Quick preview (local)

From the repo root:

```bash
cd website
python3 -m http.server 5173
```

Then open `http://localhost:5173`.

## Hook up downloads

In [website/index.html](index.html), the download buttons currently use `href="#"`.

You have two easy options:

### Option A — hardcode URLs

Replace the `href` values with wherever you host your `.dmg`/`.zip`.

### Option B — auto-link to GitHub Releases (recommended)

If you publish builds in GitHub Releases, uncomment this block in the `<head>`:

```html
<script>
  window.CONVERTABLE_RELEASES = { owner: 'amwww', repo: 'Convertable' };
</script>
```

Then make sure your release assets include names that match the buttons’ `data-asset-name`, e.g.

- `Convertable-mac-arm64.dmg`
- `Convertable-mac-x64.dmg`

The page will update download links to the latest release automatically.

## Deploy

Any static host works:

- GitHub Pages (serve the `/website` folder)
- Netlify / Vercel (set build output to `/website`, no build command needed)
- S3 + CloudFront

If using GitHub Pages, you can:

1. Create a `gh-pages` branch
2. Copy `website/*` to the branch root
3. Enable Pages for the branch
