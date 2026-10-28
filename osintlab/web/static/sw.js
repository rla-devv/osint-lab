// Service worker mínimo: torna o OSINT Lab instalável como aplicativo. Não guarda nada em cache
// (os dados ficam só no computador); as requisições seguem direto para o servidor.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", () => {});
