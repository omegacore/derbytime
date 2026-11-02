// Admin race-order list: drag (mouse or touch) or ↑/↓ to reorder; each change
// is saved immediately and the track queues update live.
(function () {
  const list = document.getElementById("race-order");
  if (!list) return;
  const url = `/admin/event/${list.dataset.event}/order`;
  let lastGood = ids();

  function ids() {
    return [...list.children].map((li) => Number(li.dataset.id));
  }

  function restore(order) {
    const byId = new Map([...list.children].map((li) => [Number(li.dataset.id), li]));
    order.forEach((id) => list.appendChild(byId.get(id)));
  }

  async function save() {
    const order = ids();
    if (order.join() === lastGood.join()) return;
    try {
      const r = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ round_ids: order }),
        credentials: "same-origin",
      });
      if (!r.ok) {
        let err = "Couldn't save the order";
        try { err = (await r.json()).error || err; } catch (_) {}
        throw new Error(err);
      }
      lastGood = order;
      RM.toast("Race order saved");
    } catch (e) {
      restore(lastGood);
      RM.toast(e.message || "Couldn't save the order", "err");
      if (/changed/.test(e.message)) setTimeout(() => location.reload(), 1500);
    }
  }

  if (window.Sortable) {
    Sortable.create(list, { handle: ".handle", animation: 150, onEnd: save });
  }

  list.addEventListener("click", (e) => {
    const b = e.target.closest("[data-move]");
    if (!b) return;
    const li = b.closest("li");
    if (b.dataset.move === "-1" && li.previousElementSibling) list.insertBefore(li, li.previousElementSibling);
    else if (b.dataset.move === "1" && li.nextElementSibling) list.insertBefore(li.nextElementSibling, li);
    b.focus();
    save();
  });
})();
