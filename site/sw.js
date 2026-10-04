/* The Silicon Commute — service worker (offline shell + saved episodes) */
const VERSION = '__BUILD__';
const SHELL = `sc-shell-${VERSION}`;
const DATA = 'sc-data';
const AUDIO = 'sc-audio';
const SHELL_FILES = ['./', 'index.html', 'app.css', 'app.js', 'manifest.webmanifest', 'icons/icon-192.png', 'icons/cover.png'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(SHELL).then((c) => c.addAll(SHELL_FILES)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k.startsWith('sc-shell-') && k !== SHELL).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

// Serve a cached MP3 with HTTP Range support (Safari requires 206 responses for media)
async function rangeResponse(request, cached) {
  const buf = await cached.arrayBuffer();
  const total = buf.byteLength;
  const range = request.headers.get('range');
  const headers = { 'Content-Type': 'audio/mpeg', 'Accept-Ranges': 'bytes' };
  if (!range) return new Response(buf, { status: 200, headers: { ...headers, 'Content-Length': String(total) } });
  const m = /bytes=(\d*)-(\d*)/.exec(range) || [];
  let start = m[1] ? parseInt(m[1], 10) : 0;
  let end = m[2] ? parseInt(m[2], 10) : total - 1;
  if (!m[1] && m[2]) { start = Math.max(0, total - parseInt(m[2], 10)); end = total - 1; }
  end = Math.min(end, total - 1);
  if (start > end || start >= total) {
    return new Response(null, { status: 416, headers: { 'Content-Range': `bytes */${total}` } });
  }
  return new Response(buf.slice(start, end + 1), {
    status: 206,
    headers: { ...headers, 'Content-Range': `bytes ${start}-${end}/${total}`, 'Content-Length': String(end - start + 1) },
  });
}

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;

  if (url.pathname.endsWith('.mp3')) {
    e.respondWith((async () => {
      const cached = await caches.match(url.href, { ignoreSearch: true });
      if (cached) return rangeResponse(req, cached);
      return fetch(req);
    })());
    return;
  }

  if (url.pathname.includes('/data/') || url.pathname.endsWith('feed.xml')) {
    // network first, fall back to the last copy we saw
    e.respondWith((async () => {
      try {
        const res = await fetch(req);
        if (res.ok) (await caches.open(DATA)).put(url.href, res.clone());
        return res;
      } catch {
        const cached = await caches.match(url.href, { ignoreSearch: true });
        return cached || new Response('{"episodes":[]}', { headers: { 'Content-Type': 'application/json' } });
      }
    })());
    return;
  }

  // app shell: network first (so app updates show up immediately), cache when offline or slow
  e.respondWith((async () => {
    const cache = await caches.open(SHELL);
    const cached = await cache.match(req, { ignoreSearch: true });
    const network = fetch(req).then((res) => { if (res.ok) cache.put(req, res.clone()); return res; });
    if (!cached) return network;
    const timeout = new Promise((resolve) => setTimeout(() => resolve(cached), 3000));
    return Promise.race([network.catch(() => cached), timeout]);
  })());
});
