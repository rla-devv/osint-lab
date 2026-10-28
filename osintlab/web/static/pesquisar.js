(function () {
  // ---- formulário: mostra o tipo identificado e pede a finalidade quando a pesquisa exige
  const q = document.getElementById("ps-q");
  if (q) {
    const tipo = document.getElementById("ps-tipo"), campo = document.getElementById("ps-campo");
    const projeto = document.getElementById("ps-projeto"), fin = document.getElementById("ps-finalidade");
    const finInput = document.getElementById("ps-fin");
    let timer = null, ultimo = "";
    function classificar() {
      const v = q.value.trim();
      const chave = v + "|" + campo.value + "|" + projeto.value;
      if (chave === ultimo) return;
      ultimo = chave;
      if (!v) { tipo.textContent = ""; fin.hidden = true; finInput.required = false; return; }
      const url = `/api/classificar?q=${encodeURIComponent(v)}&campo=${encodeURIComponent(campo.value)}` +
                  (projeto.value ? `&projeto=${projeto.value}` : "");
      fetch(url).then((r) => r.json()).then((c) => {
        if (q.value.trim() !== v) return;
        tipo.textContent = c.lista ? `Lista com ${c.lista} itens: cada um será pesquisado nas bases instaladas.` :
          c.invalido ? (c.avisos[0] || "Entrada inválida para este tipo.") : (c.rotulo ? "Identificado: " + c.rotulo : "");
        tipo.classList.toggle("erro", !!c.invalido);
        fin.hidden = !c.requer_finalidade;
        finInput.required = !!c.requer_finalidade;
      }).catch(() => {});
    }
    const agendar = () => { clearTimeout(timer); timer = setTimeout(classificar, 250); };
    const ajustarAltura = () => { q.style.height = "auto"; q.style.height = Math.min(q.scrollHeight + 2, innerHeight * 0.4) + "px";
                                  q.style.overflowY = q.scrollHeight > innerHeight * 0.4 ? "auto" : "hidden"; };
    q.addEventListener("input", ajustarAltura);
    q.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" && !ev.shiftKey && !ev.isComposing) {
        ev.preventDefault();
        if (q.value.trim()) q.form.requestSubmit();
      }
    });
    ajustarAltura();
    q.addEventListener("input", agendar);
    campo.addEventListener("change", classificar);
    projeto.addEventListener("change", classificar);
    if (q.value) classificar();
  }

  // ---- novo caso ao salvar: os campos de nome e finalidade só aparecem quando "Novo caso" está escolhido
  const sel = document.getElementById("ps-salvar-caso");
  if (sel) {
    const novo = document.querySelector(".ps-novo-caso");
    const ajustar = () => { novo.hidden = !!sel.value; };
    sel.addEventListener("change", ajustar);
    ajustar();
  }

  // ---- grafo da rede: carregado só quando o analista abre (o contêiner precisa estar visível)
  document.addEventListener("toggle", (ev) => {
    const d = ev.target;
    if (d.id !== "ps-grafo" || !d.open || d.dataset.carregado) return;
    d.dataset.carregado = "1";
    const s = document.createElement("script");
    s.src = "/static/rede.js";
    document.body.appendChild(s);
  }, true);

  // ---- resultado: filtros e atualização durante a execução
  const corpo = document.getElementById("ps-corpo");
  if (!corpo) return;
  const filtros = {};
  function aplicarFiltros() {
    corpo.querySelectorAll("[data-filtro]").forEach((el) => {
      if (filtros[el.dataset.filtro] !== undefined && el.value !== filtros[el.dataset.filtro]) el.value = filtros[el.dataset.filtro];
    });
    const nivel = filtros.nivel || "", tema = filtros.tema || "", texto = (filtros.texto || "").toLowerCase();
    corpo.querySelectorAll(".ps-ent").forEach((e) => {
      const ok = (!nivel || e.dataset.nivel === nivel) && (!tema || e.dataset.temas.split(" ").includes(tema)) &&
                 (!texto || e.textContent.toLowerCase().includes(texto));
      e.hidden = !ok;
    });
  }
  corpo.addEventListener("input", (ev) => {
    const f = ev.target.dataset && ev.target.dataset.filtro;
    if (f) { filtros[f] = ev.target.value; aplicarFiltros(); }
  });
  function emExecucao() {
    const r = corpo.querySelector(".ps-resultado");
    return r && r.dataset.estado === "executando";
  }
  function atualizar() {
    const abertos = new Set([...corpo.querySelectorAll("details[open][data-id]")].map((d) => d.dataset.id));
    const fechados = new Set([...corpo.querySelectorAll("details[data-id]:not([open])")].map((d) => d.dataset.id));
    fetch(`/pesquisar/${corpo.dataset.sid}/parcial`).then((r) => r.ok ? r.text() : Promise.reject(r.status)).then((html) => {
      corpo.innerHTML = html;
      corpo.querySelectorAll("details[data-id]").forEach((d) => {
        if (abertos.has(d.dataset.id)) d.open = true;
        else if (fechados.has(d.dataset.id)) d.open = false;
      });
      aplicarFiltros();
      if (emExecucao()) setTimeout(atualizar, 1000);
      else location.reload(); // carrega também as ações de salvar e exportar
    }).catch(() => setTimeout(atualizar, 3000));
  }
  if (emExecucao()) setTimeout(atualizar, 700);
})();
