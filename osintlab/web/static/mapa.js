(function () {
  const el = document.getElementById("mapa");
  if (!el || typeof L === "undefined") { if (el) el.textContent = "Biblioteca de mapas indisponível (sem internet?)."; return; }
  const map = L.map(el).setView([-15.8, -47.9], 4);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 19, attribution: "© OpenStreetMap" }).addTo(map);
  const layer = L.layerGroup().addTo(map);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  let data = null;
  function draw() {
    layer.clearLayers();
    const ate = document.getElementById("ate").value;
    const pts = [];
    for (const f of data.features) {
      const p = f.properties;
      if (ate && p.data && p.data.slice(0, 10) > ate) continue;
      const [lon, lat] = f.geometry.coordinates;
      pts.push([lat, lon]);
      L.circleMarker([lat, lon], { radius: 7, color: p.fonte_coordenada === "EXIF" ? "#d62728" : "#1f5fbf" }).addTo(layer)
        .bindPopup(`<b>${esc(p.titulo)}</b><br>Origem: ${esc(p.fonte_coordenada)}<br>${esc(p.rotulo)}<br>${esc(p.data || "")}<br><a href="/p/${el.dataset.pid}/item/${p.item}">abrir item #${p.item}</a>`);
    }
    if (pts.length) map.fitBounds(pts, { maxZoom: 14, padding: [30, 30] });
  }
  fetch(`/api/p/${el.dataset.pid}/geo`).then((r) => r.json()).then((d) => { data = d; draw(); });
  document.getElementById("ate").addEventListener("change", draw);
})();
