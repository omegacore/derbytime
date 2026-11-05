// Vote tablet: pick the scout, pick the vehicle, record. The form is marked
// dirty while a pick is in progress so live refreshes from other tablets
// wait instead of wiping it (see live.js busy()).
(function () {
  function filter(input) {
    const list = document.getElementById(input.dataset.for);
    if (!list) return;
    const q = input.value.trim().toLowerCase();
    for (const el of list.children) el.hidden = !!q && !el.dataset.q.includes(q);
  }

  function sync(form) {
    const voter = form.querySelector('input[name="voter"]:checked');
    const entry = form.querySelector('input[name="entry"]:checked');
    const v = voter && voter.closest(".vote-voter");
    // a scout can't vote for their own vehicle
    for (const car of form.querySelectorAll(".vote-car")) {
      const own = !!voter && car.dataset.owner === voter.value;
      car.classList.toggle("own", own);
      car.querySelector("input").disabled = own;
      if (own && entry && car.contains(entry)) entry.checked = false;
    }
    const e = form.querySelector('input[name="entry"]:checked');
    const c = e && e.closest(".vote-car");
    const replace = !!(v && v.dataset.voted);
    form.querySelector('input[name="replace"]').value = replace ? "1" : "";
    const btn = document.getElementById("vote-submit");
    const summary = document.getElementById("vote-summary");
    btn.disabled = !(v && c);
    btn.textContent = replace ? "Change vote" : "Record vote";
    let text = v ? v.dataset.name : "Pick a scout";
    text += " → " + (c ? `#${c.dataset.number}${c.dataset.name ? " " + c.dataset.name : ""}` : "pick a vehicle");
    if (replace) text += ` (already voted #${v.dataset.voted}; this replaces it)`;
    summary.textContent = text;
    if (voter || e) form.dataset.dirty = "1";
    else delete form.dataset.dirty;
  }

  document.addEventListener("input", (ev) => {
    if (ev.target.classList && ev.target.classList.contains("vote-filter")) filter(ev.target);
  });

  document.addEventListener("change", (ev) => {
    const form = ev.target.closest && ev.target.closest("#vote-form");
    if (!form) return;
    sync(form);
    if (ev.target.name === "voter") {
      const card = document.getElementById("vote-ballot-card");
      if (card) card.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  });

  // After a vote is recorded the panel comes back fresh: start the next scout
  // at the top. The confirmation moves outside the panel, because the bus
  // event for this very vote refreshes the panel again a moment later.
  document.body.addEventListener("htmx:afterSwap", (ev) => {
    if (ev.detail.target.id !== "vote-panel" || !ev.detail.requestConfig || ev.detail.requestConfig.verb !== "post") return;
    const done = ev.detail.target.querySelector(".msg.ok");
    const last = document.getElementById("vote-last");
    if (done && last) {
      last.innerHTML = done.innerHTML;
      last.hidden = false;
      done.remove();
    }
    window.scrollTo({ top: 0, behavior: "smooth" });
  });
})();
