// Assistente de IA e geração de hipóteses (sem scripts inline, por causa da CSP)
(function () {
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const md = (s) => esc(s)
    .replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")
    .replace(/\[(FATO_OBSERVADO|DOCUMENTAL|INFERENCIA|HIPOTESE|CONTRADICAO|NAO_CONFIRMADO)[^\]]*\]/g, '<span class="rot">$1</span>')
    .replace(/\n/g, "<br>");

  const chat = document.getElementById("chat");
  const form = document.getElementById("form-pergunta");
  if (chat && form) {
    const pid = chat.dataset.pid;
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const input = document.getElementById("pergunta");
      const q = input.value.trim();
      if (!q) return;
      chat.insertAdjacentHTML("beforeend", `<div class="bolha usuario">${esc(q)}</div>`);
      input.value = "";
      const wait = document.createElement("div");
      wait.className = "bolha"; wait.textContent = "Analisando fontes do projeto…";
      chat.appendChild(wait);
      try {
        const r = await fetch(`/api/p/${pid}/perguntar`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ pergunta: q }) });
        const d = await r.json();
        const fontes = (d.fontes || []).map((f) => `[${f.n}] <a href="/p/${pid}/item/${f.item}">${esc(f.titulo || "(sem título)")}</a> — ${esc(f.fonte)} (conf. ${esc(f.confiabilidade)})`).join("<br>");
        const prob = (d.problemas || []).length ? `<div class="fontes erro">Controle de qualidade: ${d.problemas.map(esc).join("; ")}</div>` : "";
        wait.innerHTML = `<div>${md(d.resposta || d.erro)}</div>${prob}<div class="fontes"><b>Fontes</b> (modo: ${esc(d.modo)})<br>${fontes}</div>`;
      } catch (e) { wait.textContent = "Erro: " + e; }
      chat.scrollTop = chat.scrollHeight;
    });
  }

  const btn = document.getElementById("gerar-hipoteses");
  if (btn) {
    btn.addEventListener("click", async () => {
      const box = document.getElementById("hipoteses");
      box.textContent = "Gerando…";
      const r = await fetch(`/api/p/${btn.dataset.pid}/hipoteses`, { method: "POST" });
      const d = await r.json();
      if (!d.hipoteses || !d.hipoteses.length) { box.innerHTML = `<p class="vazio">${esc(d.nota || "Nenhuma hipótese gerada.")}</p>`; return; }
      box.innerHTML = d.hipoteses.map((h) => `<div class="afirmacao"><span class="tag hipotese">hipótese</span> ${esc(h.enunciado)}
        <small class="meta">apoio: ${esc((h.fontes_que_apoiam || []).join(", ") || "—")} · contra: ${esc((h.fontes_que_contradizem || []).join(", ") || "—")}<br>Como verificar: ${esc(h.como_verificar)}</small></div>`).join("");
    });
  }
})();

// Progresso do download/início da IA local em Configurações
(function () {
  const painel = document.getElementById("ia-painel");
  const prog = document.getElementById("ia-progresso");
  if (!painel || !prog) return;
  const ativas = ["baixando", "verificando", "extraindo", "iniciando"];
  let fase = painel.dataset.fase;
  if (!ativas.includes(fase)) return;
  const fmt = (b) => (b / 1e9).toFixed(2) + " GB";
  const tick = async () => {
    try {
      const e = await (await fetch("/api/ia/estado")).json();
      const p = e.progresso || {};
      prog.hidden = false;
      if (p.fase === "baixando" && p.total) {
        prog.textContent = `Baixando ${p.item}: ${fmt(p.feito || 0)} de ${fmt(p.total)} (${Math.floor(100 * (p.feito || 0) / p.total)}%)`;
      } else if (p.fase === "erro") {
        prog.textContent = "Erro: " + (p.erro || "");
        prog.classList.add("erro");
        return;
      } else if (ativas.includes(p.fase)) {
        prog.textContent = ({ verificando: "Verificando integridade (SHA-256) de ", extraindo: "Preparando ", iniciando: "Carregando o modelo no " }[p.fase] || "") + (p.item || "");
      } else {
        location.reload();
        return;
      }
      setTimeout(tick, 1500);
    } catch (err) {
      setTimeout(tick, 3000);
    }
  };
  tick();
})();


// Progresso do download da transcrição em Configurações
(function () {
  const painel = document.getElementById("tr-painel");
  const prog = document.getElementById("tr-progresso");
  if (!painel || !prog) return;
  const ativas = ["baixando", "verificando", "extraindo"];
  if (!ativas.includes(painel.dataset.fase)) return;
  const fmt = (b) => (b / 1e6).toFixed(0) + " MB";
  const tick = async () => {
    try {
      const p = (await (await fetch("/api/transcricao/estado")).json()).progresso || {};
      prog.hidden = false;
      if (p.fase === "baixando" && p.total) prog.textContent = `Baixando ${p.item}: ${fmt(p.feito || 0)} de ${fmt(p.total)} (${Math.floor(100 * (p.feito || 0) / p.total)}%)`;
      else if (p.fase === "erro") { prog.textContent = "Erro: " + (p.erro || ""); prog.classList.add("erro"); return; }
      else if (ativas.includes(p.fase)) prog.textContent = (p.fase === "verificando" ? "Verificando (SHA-256) " : "Preparando ") + (p.item || "");
      else { location.reload(); return; }
      setTimeout(tick, 1500);
    } catch (err) { setTimeout(tick, 3000); }
  };
  tick();
})();
