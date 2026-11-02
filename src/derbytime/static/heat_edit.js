// Admin heat editor: reorder/add heat rows and keep the per-heat warnings and
// the races-per-scout summary current while lanes are changed. Saving is a
// plain form POST.
(function () {
  const form = document.getElementById("heat-editor");
  if (!form) return;
  const tbody = document.getElementById("heat-rows");
  const tpl = document.getElementById("new-heat");
  const summary = document.getElementById("heat-summary");
  const target = Number(form.dataset.target) || 0;
  let added = 0;

  const editable = () => [...tbody.querySelectorAll("[data-row]")];

  function refresh() {
    // heat numbers as the server will assign them: after every locked heat
    const base = Math.max(0, ...[...tbody.querySelectorAll("[data-locked] td.num")].map((td) => Number(td.textContent) || 0));
    const races = new Map(); // entry id -> [lane, ...]
    const note = (e, lane) => races.set(e, [...(races.get(e) || []), lane]);
    tbody.querySelectorAll("[data-locked] [data-entry]").forEach((td) => note(td.dataset.entry, td.dataset.lane));

    let n = 0;
    editable().forEach((tr) => {
      const picks = [...tr.querySelectorAll("select")].filter((s) => s.value);
      const counts = {};
      picks.forEach((s) => (counts[s.value] = (counts[s.value] || 0) + 1));
      picks.forEach((s) => s.classList.toggle("dup", counts[s.value] > 1));
      picks.forEach((s) => note(s.value, s.dataset.lane));
      const warn = tr.querySelector("[data-warn]");
      const dup = Object.values(counts).some((c) => c > 1);
      tr.classList.toggle("solo", picks.length === 1);
      warn.textContent = dup ? "same racer twice" : picks.length === 1 ? "only 1 racer" : picks.length === 0 ? "empty: removed on save" : "";
      warn.classList.toggle("flag", dup || picks.length === 1);
      tr.querySelector("[data-number]").textContent = picks.length ? base + ++n : "–";
    });

    const all = [...summary.querySelectorAll("tbody tr")].map((tr) => (races.get(tr.dataset.entry) || []).length);
    // without a set target, the most common count is what everyone should have
    const freq = {};
    all.filter((c) => c).forEach((c) => (freq[c] = (freq[c] || 0) + 1));
    const usual = target || Number(Object.keys(freq).sort((a, b) => freq[b] - freq[a] || b - a)[0]) || 0;
    summary.querySelectorAll("tbody tr").forEach((tr) => {
      const lanes = (races.get(tr.dataset.entry) || []).map(Number).sort((a, b) => a - b);
      const cnt = tr.querySelector("[data-count]");
      cnt.textContent = lanes.length;
      cnt.classList.toggle("flag", lanes.length !== usual);
      const repeats = lanes.filter((l, i) => lanes.indexOf(l) !== i);
      tr.querySelector("[data-lanes]").innerHTML = lanes
        .map((l) => (repeats.includes(l) ? `<span class="flag" title="lane repeated">${l}</span>` : l))
        .join(" ");
    });
  }

  tbody.addEventListener("change", refresh);

  tbody.addEventListener("click", (e) => {
    const b = e.target.closest("[data-move]");
    if (!b) return;
    const tr = b.closest("tr");
    const rows = editable();
    const i = rows.indexOf(tr);
    const other = rows[i + Number(b.dataset.move)];
    if (!other) return;
    if (b.dataset.move === "-1") tbody.insertBefore(tr, other);
    else tbody.insertBefore(other, tr);
    b.focus();
    refresh();
  });

  document.getElementById("add-heat").addEventListener("click", () => {
    const key = `n${++added}`;
    const html = tpl.innerHTML.replaceAll("KEY", key);
    tbody.insertAdjacentHTML("beforeend", html);
    tbody.lastElementChild.querySelector("select")?.focus();
    refresh();
  });

  form.addEventListener("submit", (e) => {
    if (form.querySelector("select.dup")) {
      e.preventDefault();
      RM.toast("A racer is in two lanes of the same heat", "err");
    }
  });

  refresh();
})();
