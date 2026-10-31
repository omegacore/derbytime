// Standings board: shows one slide (a den, a final, or the pack overall) at a
// time and rotates every data-secs seconds. Live refreshes replace the slides
// but keep the rotation where it was, then animate what changed on the slide
// being shown: moved rows slide from their old spot (FLIP), rows whose numbers
// changed flash, climbers get ▲n, new rows fade in. ←/→ step by hand.
(function () {
  const reduce = matchMedia("(prefers-reduced-motion: reduce)");
  let board = null;
  let current = null; // {id, group, index}
  let deadline = 0;
  let timer = null;
  let before = null;

  const slides = () => [...board.querySelectorAll(".slide")];
  const dwell = () => (+board.dataset.secs || 12) * 1000;

  // Shrink a slide's type until it fits the screen.
  function fit(slide) {
    let f = 1;
    slide.style.setProperty("--fit", f);
    // The heading doesn't scale, so step down until the table fits.
    for (let i = 0; i < 8 && f > 0.5 && slide.scrollHeight > slide.clientHeight; i++) {
      f = Math.max(0.5, f * (slide.clientHeight / slide.scrollHeight) * 0.98);
      slide.style.setProperty("--fit", f.toFixed(3));
    }
  }

  function bar(ms) {
    const el = document.getElementById("board-bar");
    if (!el) return;
    el.style.transition = "none";
    el.style.width = (100 * (1 - ms / dwell())) + "%";
    el.offsetWidth;
    el.style.transition = `width ${ms}ms linear`;
    el.style.width = "100%";
  }

  function schedule(ms) {
    clearTimeout(timer);
    const list = slides();
    if (list.length < 2) return bar(0);
    deadline = Date.now() + ms;
    bar(ms);
    timer = setTimeout(() => go(1), ms);
  }

  function show(slide, instant) {
    if (!slide) return;
    board.classList.toggle("instant", !!instant);
    slides().forEach((s) => {
      s.classList.toggle("active", s === slide);
      s.classList.toggle("entering", s === slide && !instant); // badge pop only on a real slide change
    });
    current = { id: slide.dataset.slide, group: slide.dataset.group, index: slides().indexOf(slide) };
    const track = document.getElementById("board-progress");
    if (track && slide.dataset.vehicle) track.dataset.vehicle = slide.dataset.vehicle;
    fit(slide);
    if (instant) requestAnimationFrame(() => board.classList.remove("instant"));
  }

  function go(step) {
    const list = slides();
    if (!list.length) return;
    const i = current ? list.findIndex((s) => s.dataset.slide === current.id) : -1;
    show(list[(i + step + list.length) % list.length], false);
    schedule(dwell());
  }

  function snapshot(slide) {
    const m = new Map();
    if (!slide) return m;
    slide.querySelectorAll("tr[data-key]").forEach((tr) => {
      m.set(tr.dataset.key, { top: tr.getBoundingClientRect().top, sig: tr.dataset.sig, rank: +tr.dataset.rank });
    });
    return m;
  }

  function animate(slide, old) {
    const rows = [...slide.querySelectorAll("tr[data-key]")];
    if (!old) {
      rows.forEach((tr, i) => {
        tr.style.animationDelay = Math.min(i * 45, 900) + "ms";
        tr.classList.add("enter");
      });
      return;
    }
    const moved = [];
    rows.forEach((tr) => {
      const prev = old.get(tr.dataset.key);
      if (!prev) return tr.classList.add("enter");
      const dy = prev.top - tr.getBoundingClientRect().top;
      if (dy && !reduce.matches) {
        tr.style.transition = "none";
        tr.style.transform = `translateY(${dy}px)`;
        moved.push(tr);
      }
      if (prev.sig === tr.dataset.sig) return;
      const climb = prev.rank - +tr.dataset.rank;
      tr.classList.add(climb > 0 ? "rank-up" : "changed");
      if (climb > 0) {
        const tag = document.createElement("span");
        tag.className = "climb";
        tag.textContent = "▲" + climb;
        tr.cells[0].prepend(tag);
      }
    });
    if (!moved.length) return;
    slide.offsetHeight; // commit the inverted positions before releasing them
    requestAnimationFrame(() => moved.forEach((tr) => { tr.style.transition = ""; tr.style.transform = ""; }));
  }

  document.addEventListener("htmx:beforeSwap", (e) => {
    if (e.detail.target !== board) return;
    const active = board.querySelector(".slide.active");
    before = current ? snapshot(active) : null;
    if (before) before.lead = active.querySelector(".spotlight")?.dataset.lead;
  });

  document.addEventListener("htmx:afterSwap", (e) => {
    if (e.detail.target !== board) return;
    const list = slides();
    const first = !current;
    // Stay on the same slide; if it vanished, the same den's first page, else the same position.
    const keep = current && (
      list.find((s) => s.dataset.slide === current.id) ||
      list.find((s) => s.dataset.group === current.group) ||
      list[Math.min(current.index, list.length - 1)]
    );
    show(keep || list[0], true);
    list.forEach((s) => s.classList.contains("active") || fit(s));
    const active = board.querySelector(".slide.active");
    animate(active, first ? null : before);
    const spot = active && active.querySelector(".spotlight");
    if (spot && before && spot.dataset.lead !== before.lead) spot.classList.add("new-leader");
    before = null;
    schedule(first ? dwell() : Math.max(1000, deadline - Date.now()));
  });

  // Slides entering later get the staggered entrance too.
  document.addEventListener("transitionstart", (e) => {
    if (e.target.matches && e.target.matches(".slide.active") && e.propertyName === "opacity") {
      e.target.querySelectorAll("tr.enter").forEach((tr) => tr.classList.remove("enter"));
      e.target.offsetWidth;
      animate(e.target, null);
    }
  });

  document.addEventListener("keydown", (e) => {
    if (!board) return;
    if (e.key === "ArrowRight") go(1);
    if (e.key === "ArrowLeft") go(-1);
  });
  window.addEventListener("resize", () => board && slides().forEach(fit));

  document.addEventListener("DOMContentLoaded", () => {
    board = document.querySelector("[data-board]");
    // The header (and its #conn badge) is hidden in kiosk mode; mirror it.
    const conn = document.getElementById("conn");
    const pill = document.getElementById("board-live");
    if (!conn || !pill) return;
    const sync = () => {
      const on = conn.classList.contains("on");
      pill.className = "board-live " + (on ? "on" : "off");
      pill.querySelector(".txt").textContent = on ? "LIVE" : "reconnecting…";
    };
    new MutationObserver(sync).observe(conn, { attributes: true, attributeFilter: ["class"] });
  });
})();
