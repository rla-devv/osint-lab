(function () {
  const el = document.getElementById("mapa-geo");
  if (!el) return;
  if (typeof L === "undefined") { el.textContent = "Biblioteca de mapas indisponível (sem internet?)."; return; }
  const res = JSON.parse(el.dataset.res || "{}");
  const map = L.map(el).setView([-15.8, -47.9], 3);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 19, attribution: "© OpenStreetMap" }).addTo(map);
  const pts = [];
  if (res.modo === "combinacao") {
    if (res.centro) pts.push(res.centro);
    for (const c of res.candidatos || []) {
      L.circleMarker([c.lat, c.lon], { radius: 7, color: "#d62728" }).addTo(map)
        .bindPopup(`${c.lat}, ${c.lon}<br>maior distância: ${c.maior_distancia_m} m`);
      pts.push([c.lat, c.lon]);
    }
  } else if (res.modo === "sombra") {
    const meio = (res.passo || 0.25) / 2;
    for (const [lat, lon] of res.pontos || []) {
      L.rectangle([[lat - meio, lon - meio], [lat + meio, lon + meio]], { weight: 0, fillColor: "#d62728", fillOpacity: 0.45 }).addTo(map);
      pts.push([lat, lon]);
    }
  }
  if (pts.length) map.fitBounds(pts, { maxZoom: 15, padding: [30, 30] });
})();
