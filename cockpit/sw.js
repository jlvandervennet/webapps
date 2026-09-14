// sw.js — Joe's Cockpit PWA service worker.
// NETWORK-FIRST for the app shell (index.html/manifest) so a deploy always
// reaches the device; cache is only an offline fallback. API calls pass
// through untouched (network-only, no caching). Static assets (icons) are
// cache-first. CACHE version is bumped each deploy to invalidate stale shell.
const CACHE = "cockpit-v4";
const SHELL = ["/", "/index.html", "/manifest.json"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (e) => {
  const url = e.request.url;

  // API: never cache, just forward (network-only; fail clean on offline).
  if (url.includes("/api/")) {
    e.respondWith(fetch(e.request));
    return;
  }

  // App shell: network-first so fresh HTML always wins; cache as offline net.
  if (e.request.mode === "navigate" || SHELL.indexOf(url) !== -1) {
    e.respondWith(
      fetch(e.request)
        .then((res) => {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(e.request, copy));
          return res;
        })
        .catch(() => caches.match(e.request))
    );
    return;
  }

  // Everything else (icons, static): cache-first.
  e.respondWith(
    caches.match(e.request).then((hit) => hit || fetch(e.request)
      .then((res) => {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(e.request, copy));
        return res;
      }))
  );
});