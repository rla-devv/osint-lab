// Pesquisa unificada e dossiê: a dica do campo de busca acompanha o campo escolhido
(function () {
  const f = document.getElementById("form-pesquisa");
  const q = document.getElementById("q-pesquisa");
  if (!f || !q) return;
  f.querySelectorAll("input[name=tipo]").forEach((r) => r.addEventListener("change", () => { q.placeholder = r.dataset.dica; q.focus(); }));
})();
