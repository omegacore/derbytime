// Shared race clock for start/stop presses made on different phones.
//
// Each phone estimates the server clock (NTP-style): ask /api/time a few
// times and keep the sample with the shortest round trip, so the error is at
// most half that round trip (a few ms on local Wi-Fi). Presses are then
// recorded as server-clock milliseconds and can be compared across phones.
(function () {
  const RM = (window.RM = window.RM || {});
  const local = () => performance.timeOrigin + performance.now();
  let best = null; // { offset, rtt, at }

  async function sample() {
    const t0 = local();
    const r = await fetch("/api/time", { cache: "no-store" });
    const t1 = local();
    const { now } = await r.json();
    const rtt = t1 - t0;
    // Prefer the tightest sample. Refresh a stale one every couple of minutes,
    // but never with a much slower sample: the error can be up to half the
    // round trip, and a busy server or Wi-Fi can make that seconds.
    const stale = best && t1 - best.at > 120000 && rtt <= Math.max(2 * best.rtt, 150);
    if (!best || rtt <= best.rtt || stale) best = { offset: now - (t0 + t1) / 2, rtt, at: t1 };
  }

  RM.clock = {
    now: () => local() + (best ? best.offset : 0),
    synced: () => best !== null,
    accuracyMs: () => (best ? Math.ceil(best.rtt / 2) : null),
    async sync(samples = 6) {
      for (let i = 0; i < samples; i++) {
        try { await sample(); } catch (_) { break; }
      }
      document.dispatchEvent(new CustomEvent("rm:clock"));
    },
  };

  // Elements with data-running-from="<server ms>" show a live elapsed time.
  setInterval(() => {
    document.querySelectorAll("[data-running-from]").forEach((el) => {
      const ms = RM.clock.now() - Number(el.dataset.runningFrom);
      el.textContent = (Math.max(0, ms) / 1000).toFixed(1);
    });
  }, 100);

  // Operator START button: <button data-start-heat="ID" data-track="ID">.
  // pointerdown, not click, so the press is recorded the instant it happens.
  async function pressStart(btn) {
    if (btn.disabled) return;
    const startMs = RM.clock.now();
    btn.disabled = true;
    btn.textContent = "Started";
    if (navigator.vibrate) navigator.vibrate(60);
    try {
      const r = await fetch(`/api/track/${btn.dataset.track}/start`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ heat_id: Number(btn.dataset.startHeat), start_ms: startMs }),
        credentials: "same-origin",
      });
      const body = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(body.error || "Start not recorded");
      if (body.updated === false) RM.toast(`Already started by ${body.source}. The earlier start counts.`);
      RM.refreshAll();
    } catch (e) {
      btn.disabled = false;
      btn.textContent = "START";
      RM.toast(e.message || "Start not recorded", "err");
    }
  }
  document.addEventListener("pointerdown", (e) => {
    const btn = e.target.closest("[data-start-heat]");
    if (btn) pressStart(btn);
  });
  document.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-start-heat]");
    if (btn && e.detail === 0) pressStart(btn); // keyboard activation
  });

  RM.clock.sync();
  setInterval(() => RM.clock.sync(3), 60000);
  document.addEventListener("rm:online", () => RM.clock.sync(3));
})();
