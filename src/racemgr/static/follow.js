// Follow racers without an account: the list lives in localStorage.
// Alerts arrive as "alert" bus events over the WebSocket (see services/alerts.py)
// and show as in-page banners with a beep and vibration.
(function () {
  const RM = (window.RM = window.RM || {});
  const KEY = "rm.follow";
  let audio = null;

  function load() {
    try { return JSON.parse(localStorage.getItem(KEY) || "[]"); } catch (_) { return []; }
  }
  function save(list) {
    try { localStorage.setItem(KEY, JSON.stringify(list)); } catch (_) {}
  }
  RM.following = () => load();
  RM.isFollowing = (id) => load().some((r) => r.id === id);

  function unlockAudio() {
    // Browsers only allow sound after a user gesture; Follow taps count.
    try {
      audio = audio || new (window.AudioContext || window.webkitAudioContext)();
      if (audio.state === "suspended") audio.resume();
    } catch (_) {}
  }

  function beep(kind) {
    if (!audio) return;
    const notes = kind === "racing" ? [880, 880, 880] : kind === "results" ? [660, 990] : [740, 990];
    let t = audio.currentTime;
    for (const f of notes) {
      const o = audio.createOscillator();
      const g = audio.createGain();
      o.frequency.value = f;
      g.gain.setValueAtTime(0.0001, t);
      g.gain.exponentialRampToValueAtTime(0.3, t + 0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, t + 0.22);
      o.connect(g).connect(audio.destination);
      o.start(t);
      o.stop(t + 0.25);
      t += 0.28;
    }
  }

  RM.toggleFollow = function (id, name) {
    unlockAudio();
    let list = load();
    if (list.some((r) => r.id === id)) list = list.filter((r) => r.id !== id);
    else list.push({ id, name });
    save(list);
    syncButtons();
    renderPanel();
  };

  function syncButtons() {
    document.querySelectorAll("[data-follow]").forEach((b) => {
      const on = RM.isFollowing(Number(b.dataset.follow));
      b.textContent = on ? "★ Following" : "☆ Follow";
      b.classList.toggle("primary", on);
    });
  }

  const STATUS_TEXT = { racing: "Racing now", on_deck: "On deck", two_away: "2 heats away" };

  let panelTimer = null;
  async function renderPanel() {
    panelTimer = null;
    const panel = document.getElementById("follow-panel");
    if (!panel) return;
    const list = load();
    if (!list.length) {
      panel.innerHTML = '<p class="muted small">Tap ☆ Follow on a racer to get alerts when they are up.</p>';
      return;
    }
    let info = {};
    try {
      const r = await fetch("/api/racers?ids=" + list.map((x) => x.id).join(","));
      if (r.ok) for (const x of (await r.json()).racers) info[x.id] = x;
    } catch (_) {}
    panel.innerHTML = "";
    for (const f of list) {
      const x = info[f.id] || { name: f.name };
      const st = x.status;
      const row = document.createElement("div");
      row.className = "row spread";
      row.style.padding = "6px 0";
      const a = document.createElement("a");
      a.href = "/racer/" + f.id;
      a.textContent = x.name || f.name;
      const s = document.createElement("span");
      s.className = "small " + (st ? "badge " + (st.kind === "racing" ? "bad" : "pending") : "muted");
      s.textContent = st ? `${STATUS_TEXT[st.kind]} · ${st.track_name} heat ${st.heat_number}, lane ${st.lane}` : "";
      const b = document.createElement("button");
      b.className = "link small";
      b.textContent = "unfollow";
      b.onclick = () => RM.toggleFollow(f.id, f.name);
      row.append(a, s, b);
      panel.appendChild(row);
    }
  }
  RM.renderFollowPanel = renderPanel;

  function banner(d) {
    const box = document.getElementById("alerts");
    if (!box) return;
    const el = document.createElement("div");
    el.className = "alert " + d.kind;
    const text = document.createElement("span");
    text.textContent = d.kind === "results" ? `${d.message}: ${d.time}, place ${d.place}` : d.message;
    const close = document.createElement("button");
    close.textContent = "OK";
    close.onclick = () => el.remove();
    el.append(text, close);
    box.prepend(el);
    while (box.children.length > 3) box.lastChild.remove();
    setTimeout(() => el.remove(), d.kind === "racing" ? 30000 : 15000);
  }

  document.addEventListener("rm:event", (e) => {
    const msg = e.detail;
    if (msg.type === "alert" && RM.isFollowing(msg.data.racer_id)) {
      banner(msg.data);
      beep(msg.data.kind);
      if (navigator.vibrate) navigator.vibrate(msg.data.kind === "racing" ? [200, 100, 200] : 150);
    }
    // News about a followed racer refreshes the panel promptly; anything else
    // (e.g. the queue shifting on another track) only lazily, spread out, so
    // a hundred phones don't all ask the server after every heat.
    const mine = load().some((r) => (msg.keys || []).includes("racer-" + r.id));
    if (mine) {
      clearTimeout(panelTimer);
      panelTimer = setTimeout(renderPanel, 300);
    } else if (msg.type === "track.state" && !panelTimer) {
      panelTimer = setTimeout(renderPanel, 5000 + Math.random() * 10000);
    }
  });
  document.addEventListener("DOMContentLoaded", () => {
    syncButtons();
    renderPanel();
    document.body.addEventListener("htmx:afterSwap", syncButtons);
    document.addEventListener("click", unlockAudio, { once: true });
    document.addEventListener("click", (e) => {
      const b = e.target.closest("[data-follow]");
      if (b) RM.toggleFollow(Number(b.dataset.follow), b.dataset.name || "");
    });
  });
})();
