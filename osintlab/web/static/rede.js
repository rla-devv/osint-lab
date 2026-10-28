(function () {
  const el = document.getElementById("rede");
  const dados = document.getElementById("rede-dados");
  if (!el || !dados) return;
  if (typeof vis === "undefined") { el.textContent = "Biblioteca de grafos indisponível (sem internet?). As tabelas abaixo têm a rede completa."; return; }
  const r = JSON.parse(dados.textContent);
  const cor = { empresa: "#2a6fd6", pessoa: "#16794a", estrangeiro: "#c77700" };
  const forma = { empresa: "box", pessoa: "dot", estrangeiro: "triangle" };
  const nodes = new vis.DataSet(r.nos.map((n) => ({ id: n.id, label: n.rotulo.length > 34 ? n.rotulo.slice(0, 32) + "…" : n.rotulo,
    shape: forma[n.tipo] || "dot", color: n.raiz ? "#d62728" : cor[n.tipo], font: { color: n.tipo === "empresa" ? "#fff" : undefined },
    size: n.tipo === "empresa" ? undefined : 8 + 4 * Math.min(n.empresas || 1, 6), meta: n })));
  const edges = new vis.DataSet(r.arestas.map((e, i) => ({ id: i, from: e.de, to: e.para, arrows: "to", title: `${e.qualificacao} desde ${e.desde}`,
    dashes: e.nivel !== "documental", color: e.nivel === "documental" ? "#5a6675" : "#1f5fbf" })));
  const net = new vis.Network(el, { nodes, edges }, { physics: { stabilization: { iterations: 250 } }, interaction: { hover: true }, edges: { smooth: false } });
  const det = document.getElementById("rede-detalhe");
  net.on("click", (p) => {
    if (!p.nodes.length) return;
    const n = nodes.get(p.nodes[0]).meta;
    const t = document.createElement("span");
    t.textContent = n.tipo === "empresa" ? `${n.rotulo} · CNPJ ${n.cnpj} · ${n.situacao} · ${n.municipio}`
      : `${n.rotulo}${n.documento ? " · CPF parcial " + n.documento : ""}${n.empresas > 1 ? " · em " + n.empresas + " empresas" : ""}`;
    det.replaceChildren(t);
    if (n.tipo === "empresa" && n.cnpj && !n.raiz) {
      const a = document.createElement("a");
      if (location.pathname.startsWith("/pesquisar")) {
        a.href = `/pesquisar?q=${encodeURIComponent(n.cnpj)}`; a.textContent = " — pesquisar esta empresa";
      } else {
        const pid = location.pathname.split("/")[2];
        a.href = `/p/${pid}/rede?q=${encodeURIComponent(n.cnpj)}`; a.textContent = " — abrir a rede desta empresa";
      }
      det.appendChild(a);
    }
  });
})();
