(function () {
  const el = document.getElementById("grafo");
  if (!el || typeof vis === "undefined") { if (el) el.textContent = "Biblioteca de grafos indisponível (sem internet?). Use a exportação GraphML."; return; }
  const pid = el.dataset.pid;
  const palette = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd", "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf"];
  const shapes = { dominio: "diamond", email: "triangle", ipv4: "square", ipv6: "square", organizacao: "dot", pessoa: "star", cnpj: "hexagon", local: "triangleDown" };
  const edgeColor = { confirmado: "#16794a", possivel: "#1f5fbf", hipotese: "#c77700", coincidencia: "#999" };
  let network;
  async function load() {
    const nivel = document.getElementById("nivel").value;
    const sens = document.getElementById("sensiveis").checked ? 1 : 0;
    const d = await (await fetch(`/api/p/${pid}/grafo?nivel=${nivel}&sensiveis=${sens}`)).json();
    const nodes = new vis.DataSet(d.nodes.map((n) => ({ id: n.id, label: n.label, title: `${n.tipo}: ${n.label}`, shape: shapes[n.tipo] || "dot",
      size: 10 + 40 * n.centralidade, color: n.comunidade >= 0 ? palette[n.comunidade % palette.length] : "#888", tipo: n.tipo })));
    const edges = new vis.DataSet(d.edges.map((e, i) => ({ id: i, from: e.from, to: e.to, title: `${e.nivel}: ${e.tipo}`, color: edgeColor[e.nivel],
      dashes: e.nivel === "hipotese" || e.nivel === "coincidencia", width: 1 + 3 * (e.score || 0), meta: e })));
    network = new vis.Network(el, { nodes, edges }, { physics: { stabilization: { iterations: 200 } }, interaction: { hover: true }, edges: { smooth: false } });
    const det = document.getElementById("detalhe");
    network.on("click", (p) => {
      if (p.nodes.length) { const n = nodes.get(p.nodes[0]); det.innerHTML = `<b>${n.tipo}</b>: ${n.label} — <a href="/p/${pid}/entidade/${n.id}">abrir entidade</a>`; }
      else if (p.edges.length) { const e = edges.get(p.edges[0]).meta; det.textContent = `[${e.nivel}] ${e.tipo}: ${e.justificativa}`; }
    });
  }
  document.getElementById("recarregar").addEventListener("click", load);
  load();
})();
