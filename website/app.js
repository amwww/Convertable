/*
  Lightweight helper:
  - Detect OS/arch to highlight best download
  - Set current year
  - Optional: fetch latest GitHub release (disabled by default)
*/

function detectPlatform() {
  const ua = navigator.userAgent || '';
  const platform = (navigator.platform || '').toLowerCase();

  const isMac = platform.includes('mac') || ua.includes('Macintosh');
  const isWindows = platform.includes('win') || ua.includes('Windows');
  const isLinux = platform.includes('linux') || ua.includes('Linux');

  // Apple Silicon detection is imperfect in browsers. This is best-effort.
  const isAppleSilicon = ua.includes('ARM') || ua.includes('arm') || ua.includes('Apple');

  return { isMac, isWindows, isLinux, isAppleSilicon };
}

function setYear() {
  const el = document.getElementById('year');
  if (el) el.textContent = String(new Date().getFullYear());
}

function highlightBestDownload() {
  const p = detectPlatform();
  const best = document.querySelector('[data-best-download]');
  if (!best) return;

  if (p.isMac) {
    best.textContent = p.isAppleSilicon ? 'Best for: macOS (Apple Silicon)' : 'Best for: macOS';
    return;
  }
  if (p.isWindows) {
    best.textContent = 'Best for: Windows (coming soon)';
    return;
  }
  if (p.isLinux) {
    best.textContent = 'Best for: Linux (coming soon)';
    return;
  }
  best.textContent = 'Best for: macOS';
}

function wireSmoothScroll() {
  document.querySelectorAll('a[href^="#"]').forEach((a) => {
    a.addEventListener('click', (e) => {
      const href = a.getAttribute('href') || '';
      if (!href || href === '#') return;
      const target = document.querySelector(href);
      if (!target) return;
      e.preventDefault();
      target.scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
  });
}

// Optional: GitHub Releases integration.
// If you host downloads on GitHub Releases, you can enable this by setting
// window.CONVERTABLE_RELEASES = { owner: 'amwww', repo: 'Convertable' } in index.html.
async function maybeLoadLatestRelease() {
  const cfg = window.CONVERTABLE_RELEASES;
  if (!cfg || !cfg.owner || !cfg.repo) return;

  try {
    const url = `https://api.github.com/repos/${cfg.owner}/${cfg.repo}/releases/latest`;
    const res = await fetch(url, { headers: { 'Accept': 'application/vnd.github+json' } });
    if (!res.ok) return;
    const data = await res.json();

    const tag = typeof data.tag_name === 'string' ? data.tag_name : '';
    const el = document.querySelector('[data-latest-version]');
    if (el && tag) el.textContent = tag;

    const assets = Array.isArray(data.assets) ? data.assets : [];

    const byName = new Map();
    for (const a of assets) {
      if (!a || typeof a.name !== 'string' || typeof a.browser_download_url !== 'string') continue;
      byName.set(a.name, a.browser_download_url);
    }

    function findByPattern(pattern) {
      if (!pattern) return '';
      let re;
      try {
        re = new RegExp(pattern);
      } catch {
        return '';
      }
      for (const a of assets) {
        if (!a || typeof a.name !== 'string' || typeof a.browser_download_url !== 'string') continue;
        if (re.test(a.name)) return a.browser_download_url;
      }
      return '';
    }

    // Update hrefs if matching assets exist.
    document.querySelectorAll('a[data-asset-name], a[data-asset-pattern]').forEach((link) => {
      const name = link.getAttribute('data-asset-name') || '';
      const pattern = link.getAttribute('data-asset-pattern') || '';

      let dl = '';
      if (name) dl = byName.get(name) || '';
      if (!dl && pattern) dl = findByPattern(pattern);

      if (typeof dl === 'string' && dl) link.setAttribute('href', dl);
    });
  } catch {
    // ignore
  }
}

window.addEventListener('DOMContentLoaded', () => {
  setYear();
  highlightBestDownload();
  wireSmoothScroll();
  void maybeLoadLatestRelease();
});
