import fs from 'node:fs/promises';
import path from 'node:path';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import sharp from 'sharp';
import pngToIco from 'png-to-ico';

const execFileAsync = promisify(execFile);

const repoRoot = path.resolve(process.cwd());
const srcSvg = path.join(repoRoot, 'website', 'assets', 'logo.svg');
const iconsetDir = path.join(repoRoot, 'build-resources', 'icon.iconset');
const outIcns = path.join(repoRoot, 'build-resources', 'icon.icns');
const outPng = path.join(repoRoot, 'build-resources', 'icon.png');
const outIco = path.join(repoRoot, 'build-resources', 'icon.ico');

const iconsetFiles = [
  { size: 16, scale: 1 },
  { size: 16, scale: 2 },
  { size: 32, scale: 1 },
  { size: 32, scale: 2 },
  { size: 128, scale: 1 },
  { size: 128, scale: 2 },
  { size: 256, scale: 1 },
  { size: 256, scale: 2 },
  { size: 512, scale: 1 },
  { size: 512, scale: 2 },
];

function iconFilename(size, scale) {
  if (scale === 1) return `icon_${size}x${size}.png`;
  return `icon_${size}x${size}@2x.png`;
}

async function renderPng(svgBuffer, px) {
  return sharp(svgBuffer, { density: 512 })
    .resize(px, px)
    .png({ compressionLevel: 9 })
    .toBuffer();
}

async function main() {
  const svg = await fs.readFile(srcSvg);

  // Linux icon
  await fs.mkdir(path.dirname(outPng), { recursive: true });
  await fs.writeFile(outPng, await renderPng(svg, 512));
  console.log(`Wrote ${path.relative(repoRoot, outPng)}`);

  // Windows icon (.ico)
  const icoPngs = await Promise.all([
    renderPng(svg, 16),
    renderPng(svg, 32),
    renderPng(svg, 48),
    renderPng(svg, 64),
    renderPng(svg, 128),
    renderPng(svg, 256),
  ]);
  await fs.writeFile(outIco, await pngToIco(icoPngs));
  console.log(`Wrote ${path.relative(repoRoot, outIco)}`);

  // macOS icon (.icns) via iconutil (macOS only)
  if (process.platform === 'darwin') {
    await fs.rm(iconsetDir, { recursive: true, force: true });
    await fs.mkdir(iconsetDir, { recursive: true });
    for (const { size, scale } of iconsetFiles) {
      const px = size * scale;
      const outPath = path.join(iconsetDir, iconFilename(size, scale));
      await fs.writeFile(outPath, await renderPng(svg, px));
    }
    await fs.rm(outIcns, { force: true });
    await execFileAsync('iconutil', ['-c', 'icns', iconsetDir, '-o', outIcns]);
    console.log(`Wrote ${path.relative(repoRoot, outIcns)}`);
  } else {
    console.log('Skipped .icns (iconutil only available on macOS)');
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
