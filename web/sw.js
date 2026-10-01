// Minimal service worker: makes the player installable as an app on
// Android/desktop browsers. It caches nothing and lets every request go to
// the network, so the live stream and playlist are never served stale.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', () => {});
