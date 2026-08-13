// Service worker toi gian cho phep trinh duyet cho phep "cai dat" trang
// nay nhu 1 app that (PWA). Chi cache giao dien tinh (khong cache API
// /api/analyze vi du lieu gia vang can luon moi nhat).
const CACHE_NAME = "gold-analysis-shell-v1";
const SHELL_FILES = [
  "/",
  "/index.html",
  "/manifest.json",
  "/icons/icon-192.png",
  "/icons/icon-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_FILES))
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);

  // KHONG cache API phan tich - luon lay du lieu gia vang moi nhat tu mang
  if (url.pathname.startsWith("/api/")) {
    return;
  }

  // Cac file giao dien tinh: uu tien cache, du phong mang khi offline
  event.respondWith(
    caches.match(event.request).then((cached) => {
      return (
        cached ||
        fetch(event.request).then((response) => {
          const clone = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(event.request, clone));
          return response;
        }).catch(() => cached)
      );
    })
  );
});
