// WebSocket client + live panel refresh + toasts. Loaded on every page.
//
// Panels declare what they care about: <div data-live="track-3 heat" hx-get=...
// hx-trigger="load, rm-load">. Every bus event carries routing keys; when a
// key matches, the panel re-fetches its partial. Panels with a focused or
// edited input wait until the user is done so we never clobber typing.
(function () {
  const RM = (window.RM = window.RM || {});
  let ws = null;
  let retry = 1000;
  let pingTimer = null;
  const timers = new WeakMap();

  RM.toast = function (text, kind) {
    const box = document.getElementById("toast");
    if (!box) return alert(text);
    const el = document.createElement("div");
    el.className = "t" + (kind === "err" ? " err" : "");
    el.textContent = text;
    box.appendChild(el);
    setTimeout(() => el.remove(), kind === "err" ? 6000 : 3000);
  };

  function setConn(on) {
    const el = document.getElementById("conn");
    if (el) {
      el.className = on ? "on" : "off";
      el.textContent = on ? "live" : "reconnecting…";
    }
    RM.online = on;
  }

  function busy(el) {
    const a = document.activeElement;
    if (a && el.contains(a) && /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName)) return true;
    return !!el.querySelector("[data-dirty]");
  }

  function refresh(el) {
    if (busy(el)) {
      el.dataset.stale = "1";
      return;
    }
    // Public panels set data-jitter (ms) so a hundred phones don't all
    // re-fetch in the same instant after every event.
    if (timers.get(el)) return; // a refresh is already on its way
    const delay = 120 + Math.random() * (+el.dataset.jitter || 0);
    timers.set(el, setTimeout(() => {
      timers.delete(el);
      delete el.dataset.stale;
      if (window.htmx) htmx.trigger(el, "rm-load");
    }, delay));
  }
  RM.refresh = refresh;

  RM.refreshAll = function () {
    document.querySelectorAll("[data-live]").forEach(refresh);
  };

  function matches(el, keys) {
    const want = el.dataset.live.split(/\s+/);
    return want.includes("*") || want.some((k) => keys.includes(k));
  }

  function handle(msg) {
    document.dispatchEvent(new CustomEvent("rm:event", { detail: msg }));
    const keys = msg.keys || [];
    if (!keys.length) return;
    document.querySelectorAll("[data-live]").forEach((el) => {
      if (matches(el, keys)) refresh(el);
    });
  }

  function connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${location.host}/ws`);
    ws.onopen = () => {
      retry = 1000;
      setConn(true);
      RM.refreshAll(); // catch up on anything missed while disconnected
      document.dispatchEvent(new CustomEvent("rm:online"));
      clearInterval(pingTimer);
      pingTimer = setInterval(() => ws.readyState === 1 && ws.send("ping"), 25000);
    };
    ws.onmessage = (e) => {
      try { handle(JSON.parse(e.data)); } catch (err) { console.error(err); }
    };
    ws.onclose = () => {
      setConn(false);
      clearInterval(pingTimer);
      setTimeout(connect, retry);
      retry = Math.min(retry * 2, 10000);
    };
    ws.onerror = () => ws.close();
  }

  // Forms: mark dirty while editing so live refreshes wait.
  document.addEventListener("input", (e) => {
    const f = e.target.closest && e.target.closest("form");
    if (f && f.closest("[data-live]")) f.dataset.dirty = "1";
  });
  document.addEventListener("focusout", () => {
    setTimeout(() => {
      document.querySelectorAll("[data-live][data-stale]").forEach((el) => !busy(el) && refresh(el));
    }, 50);
  });
  document.addEventListener("click", (e) => {
    // Cancel buttons inside live panels drop the dirty flag
    if (e.target.matches("[data-cancel]")) {
      const f = e.target.closest("form");
      if (f) { delete f.dataset.dirty; f.reset(); }
      const det = e.target.closest("details");
      if (det) det.open = false;
      const panel = e.target.closest("[data-live]");
      if (panel && panel.dataset.stale) refresh(panel);
    }
  });

  document.addEventListener("DOMContentLoaded", () => {
    document.body.addEventListener("rm-refresh", RM.refreshAll);
    document.body.addEventListener("htmx:afterRequest", (e) => {
      const f = e.detail.elt && e.detail.elt.closest && e.detail.elt.closest("form");
      if (f && e.detail.successful) delete f.dataset.dirty;
    });
    document.body.addEventListener("htmx:responseError", (e) => {
      let text = e.detail.xhr.responseText;
      try { text = JSON.parse(text).error || text; } catch (_) {}
      RM.toast(text || "Request failed", "err");
    });
    document.body.addEventListener("htmx:sendError", () => RM.toast("Can't reach the server", "err"));
    connect();
  });
})();
