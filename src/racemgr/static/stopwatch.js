// Regatta lane-official stopwatch with a shared start.
//
// The heat's start is the earliest START press by the operator or any lane
// official; the server relays it to every phone. When it arrives this clock
// starts running by itself and the big button becomes STOP, so a lane
// official normally only presses STOP. They may also press START themselves
// (e.g. no operator, or their phone is offline); that press is sent too and
// counts if it was the earliest. Resume undoes an accidental STOP.
//
// Presses are recorded on the server clock (see clock.js) and only the STOP
// moment is submitted; the server computes stop − start. Results that can't
// be sent are queued in localStorage and retried on reconnect. Wake Lock and
// vibration are used when the browser offers them (Wake Lock needs HTTPS).
(function () {
  const root = document.getElementById("stopwatch");
  if (!root) return;
  const TRACK = Number(root.dataset.track);
  const LANE = Number(root.dataset.lane);
  const QKEY = `rm.lanequeue.${TRACK}.${LANE}`;
  const DONEKEY = `rm.lanedone.${TRACK}.${LANE}`;

  const $ = (id) => document.getElementById(id);
  const timeEl = $("sw-time"), big = $("sw-big"), submitBtn = $("sw-submit"), resumeBtn = $("sw-resume"),
    dnfBtn = $("sw-dnf"), statusEl = $("sw-status"), wakeEl = $("sw-wake");

  let state = "idle"; // idle | running | stopped
  let heatId = null;   // heat this stopwatch is timing
  let shared = null;   // relayed earliest start for heatId (server ms)
  let sharedBy = "";
  let myStart = null;  // this official's own START press (server ms)
  let stopAt = null;   // STOP press (server ms)
  let raf = 0, wakeLock = null;

  const store = {
    get(k, d) { try { return JSON.parse(localStorage.getItem(k)) ?? d; } catch (_) { return d; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (_) {} },
  };
  const queue = () => store.get(QKEY, []);
  const done = () => store.get(DONEKEY, []);
  const markDone = (h) => store.set(DONEKEY, [...done().filter((x) => x !== h), h].slice(-50));
  const unmarkDone = (h) => store.set(DONEKEY, done().filter((x) => x !== h));

  const fmt = (ms) => (Math.max(0, ms) / 1000).toFixed(2);
  const now = () => (window.RM && RM.clock ? RM.clock.now() : Date.now());
  const buzz = (p) => navigator.vibrate && navigator.vibrate(p);
  const newId = () => (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random());

  function info() {
    const el = $("lane-info-data");
    if (!el) return {};
    return {
      heat: Number(el.dataset.heat) || null,
      armed: el.dataset.armed === "1",
      occupied: el.dataset.occupied === "1",
      hasResult: el.dataset.hasResult === "1",
      start: el.dataset.start ? Number(el.dataset.start) : null,
      startBy: el.dataset.startSource || "",
    };
  }

  // the earliest start we know of
  function start() {
    const s = [shared, myStart].filter((x) => x !== null);
    return s.length ? Math.min(...s) : null;
  }

  async function lockScreen() {
    if (!("wakeLock" in navigator)) {
      wakeEl.textContent = window.isSecureContext ? "Screen may sleep (no Wake Lock support)" : "Screen may sleep (Wake Lock needs HTTPS)";
      return;
    }
    try {
      wakeLock = await navigator.wakeLock.request("screen");
      wakeEl.textContent = "Screen will stay on";
      wakeLock.addEventListener("release", () => { wakeEl.textContent = "Screen may sleep"; });
    } catch (_) { wakeEl.textContent = "Screen may sleep"; }
  }
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible" && wakeLock) lockScreen(); });

  function tick() {
    cancelAnimationFrame(raf);
    const loop = () => {
      if (state !== "running") return;
      timeEl.textContent = fmt(now() - start());
      raf = requestAnimationFrame(loop);
    };
    loop();
  }

  function begin() {
    state = "running";
    buzz(60);
    lockScreen();
    tick();
  }

  // The operator disarmed the heat or called a re-run: throw this attempt away.
  // starts from voided attempts; the panel may still show one until it refreshes
  const voided = new Set();

  function resetLocal(why) {
    const h = heatId;
    [shared, myStart, info().start].forEach((t) => t !== null && t !== undefined && voided.add(t));
    cancelAnimationFrame(raf);
    if (h) {
      unmarkDone(h);
      store.set(QKEY, queue().filter((item) => item.heat_id !== h)); // unsent results for a voided attempt
    }
    heatId = null;
    shared = myStart = stopAt = null;
    sharedBy = "";
    state = "idle";
    timeEl.textContent = "0.00";
    statusEl.textContent = why;
    buzz([120, 80, 120]);
  }

  // Follow the heat on the track: new heat -> fresh stopwatch; relayed start -> run.
  function adopt() {
    const i = info();
    // missed the reset message? our heat is no longer armed, so it was reset
    if (heatId && i.heat === heatId && !i.armed && state !== "idle") {
      resetLocal("The heat was disarmed. Wait for it to be armed again.");
    }
    if (i.heat !== heatId && state !== "stopped") {
      heatId = i.heat;
      shared = myStart = stopAt = null;
      sharedBy = "";
      state = "idle";
      cancelAnimationFrame(raf);
      timeEl.textContent = "0.00";
    }
    if (i.heat && i.heat === heatId && i.start !== null && !voided.has(i.start)) {
      shared = i.start;
      sharedBy = i.startBy;
    }
    const already = heatId && (done().includes(heatId) || i.hasResult);
    if (state === "idle" && i.armed && i.occupied && !already && start() !== null) begin();
    if (state === "stopped") timeEl.textContent = fmt(stopAt - start()); // start may have been corrected
    render();
  }

  function render() {
    const i = info();
    const already = heatId && (done().includes(heatId) || i.hasResult);
    big.className = "sw-big";
    if (state === "running") {
      big.textContent = "STOP"; big.classList.add("stop"); big.disabled = false;
    } else if (state === "stopped") {
      big.textContent = fmt(stopAt - start()); big.classList.add("wait"); big.disabled = true;
    } else if (i.armed && i.occupied && !already) {
      big.textContent = "START"; big.classList.add("start"); big.disabled = false;
    } else {
      big.textContent = already ? "Sent ✓" : i.occupied === false ? "No boat" : i.heat ? "Wait for arm…" : "Waiting…";
      big.classList.add("wait"); big.disabled = true;
    }
    submitBtn.disabled = state !== "stopped";
    resumeBtn.disabled = state !== "stopped";
    dnfBtn.disabled = !(state !== "idle" || (i.armed && i.occupied && !already));
    const by = shared !== null && (myStart === null || shared <= myStart) ? sharedBy : myStart !== null ? "you" : "";
    if (state === "running" || state === "stopped") {
      statusEl.textContent = by ? `Started by ${by}` : "";
    }
    const pend = queue().length;
    if (pend) statusEl.textContent = `${pend} result(s) waiting to send…`;
  }

  async function sendStart(startMs) {
    try {
      await fetch(`/api/track/${TRACK}/start`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ heat_id: heatId, start_ms: startMs }),
        credentials: "same-origin",
      });
    } catch (_) {
      // offline: the start goes along with the queued result instead
    }
  }

  function press() {
    if (big.disabled) return;
    const t = now(); // capture first, before any other work
    if (state === "idle") {
      const i = info();
      if (!i.armed) return;
      heatId = i.heat;
      myStart = t;
      begin();
      sendStart(t);
    } else if (state === "running") {
      stopAt = t;
      state = "stopped";
      cancelAnimationFrame(raf);
      timeEl.textContent = fmt(stopAt - start());
      buzz([40, 60, 40]);
    }
    render();
  }
  // pointerdown records the press the instant the finger lands
  big.addEventListener("pointerdown", press);
  big.addEventListener("click", (e) => { if (e.detail === 0) press(); }); // keyboard

  // Resume: undo an accidental STOP. The race is still on, so keep timing
  // from the same start. (A bad start is the operator's job: Disarm resets
  // every lane phone.)
  resumeBtn.addEventListener("click", () => {
    if (state !== "stopped") return;
    stopAt = null;
    state = "running";
    tick();
    render();
  });

  function enqueue(item) { store.set(QKEY, [...queue(), item]); }

  async function send(item) {
    const r = await fetch(`/api/track/${TRACK}/lane/${LANE}/time`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(item),
      credentials: "same-origin",
    });
    if (r.ok) return { ok: true, body: await r.json() };
    let err = "Rejected";
    try { err = (await r.json()).error || err; } catch (_) {}
    return { ok: false, final: r.status >= 400 && r.status < 500, err };
  }

  function sentText(body, dnf) {
    if (dnf) return "Sent DNF ✓";
    if (body && body.time != null) return `Sent ${body.time.toFixed(2)} s ✓`;
    return "Sent ✓ The time appears once the start is known.";
  }

  async function submit(dnf) {
    const h = heatId || info().heat;
    if (!h) return;
    const item = { heat_id: h, submission_id: newId() };
    if (!dnf) {
      item.stop_ms = stopAt;
      if (myStart !== null) item.start_ms = myStart;
    }
    state = "idle";
    cancelAnimationFrame(raf);
    timeEl.textContent = dnf ? "DNF" : fmt(stopAt - start());
    markDone(h);
    try {
      const res = await send(item);
      if (res.ok) { statusEl.textContent = sentText(res.body, dnf); buzz(30); }
      else if (res.final) { statusEl.textContent = res.err; RM.toast(res.err, "err"); unmarkDone(h); }
      else { enqueue(item); statusEl.textContent = "Saved on this phone; will retry…"; }
    } catch (_) {
      enqueue(item);
      statusEl.textContent = "No connection. Saved on this phone; will retry…";
    }
    render();
  }

  submitBtn.addEventListener("click", () => submit(false));
  dnfBtn.addEventListener("click", () => {
    if (!heatId) heatId = info().heat;
    if (confirm("Record DNF for this lane?")) submit(true);
  });

  let flushing = false;
  async function flush() {
    if (flushing) return;
    flushing = true;
    try {
      let q = queue();
      while (q.length) {
        let res;
        try { res = await send(q[0]); } catch (_) { break; }
        if (!res.ok && !res.final) break;
        if (res.ok) statusEl.textContent = "Queued result sent ✓";
        else { statusEl.textContent = `Queued result rejected: ${res.err}`; RM.toast(res.err, "err"); }
        q = queue().slice(1);
        store.set(QKEY, q);
      }
    } finally { flushing = false; render(); }
  }

  // The relayed start (and resets) arrive over the WebSocket before the panel refresh.
  document.addEventListener("rm:event", (e) => {
    const m = e.detail;
    if (m.type === "heat.reset" && m.data.track_id === TRACK) {
      const mine = m.data.heat_id === heatId || m.data.heat_id === info().heat;
      if (mine) {
        const rerun = m.data.new_heat_id != null;
        resetLocal(rerun ? `Re-run called (${m.data.reason.replace(/^re-run: /, "")}). Wait for the heat to be armed.` : "The operator reset the heat. Wait for it to be armed again.");
        render(); // the panel refresh that follows brings the heat's new state
      }
      return;
    }
    if (m.type === "heat.started" && voided.has(m.data.start_ms)) return;
    if (m.type === "heat.started" && m.data.track_id === TRACK) {
      if (heatId === null) heatId = info().heat;
      if (m.data.heat_id === heatId) {
        shared = m.data.start_ms;
        sharedBy = m.data.source;
        adopt();
      }
    }
  });
  document.addEventListener("rm:online", flush);
  document.addEventListener("rm:clock", render);
  window.addEventListener("online", flush);
  setInterval(() => queue().length && flush(), 5000);
  document.body.addEventListener("htmx:afterSwap", adopt);
  adopt();
  flush();
})();
