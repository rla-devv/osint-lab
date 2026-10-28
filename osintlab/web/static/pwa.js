// Registra o service worker (aplicativo instalável; o navegador só aceita em HTTPS ou no próprio computador).
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(() => {});
}
