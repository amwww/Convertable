import fs from 'node:fs/promises';
import path from 'node:path';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import sharp from 'sharp';

const execFileAsync = promisify(execFile);

const repoRoot = path.resolve(process.cwd());
const srcSvg = path.join(repoRoot, 'website', 'assets', 'logo.svg');
const iconsetDir = path.join(repoRoot, 'build-resources', 'icon.iconset');
const outIcns = path.join(repoRoot, 'build-resources', 'icon.icns');

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

async function main() {
  const svg = await fs.readFile(srcSvg);

  await fs.rm(iconsetDir, { recursive: true, force: true });
  await fs.mkdir(iconsetDir, { recursive: true });

  for (const { size, scale } of iconsetFiles) {
    const px = size * scale;
    const outPath = path.join(iconsetDir, iconFilename(size, scale));
    const png = await sharp(svg, { density: 512 })
      .resize(px, px)
      .png({ compressionLevel: 9 })
      .toBuffer();
    await fs.writeFile(outPath, png);
  }

  await fs.rm(outIcns, { force: true });
  await execFileAsync('iconutil', ['-c', 'icns', iconsetDir, '-o', outIcns]);

  console.log(`Wrote ${path.relative(repoRoot, outIcns)}`);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
