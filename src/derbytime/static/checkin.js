// Check-in photos: tapping a photo box opens the phone camera. The picture is
// shrunk on the phone before upload so it goes quickly over venue Wi-Fi.
(function () {
  const MAX_SIDE = 1600;

  async function shrink(file) {
    try {
      const bmp = await createImageBitmap(file, { imageOrientation: "from-image" });
      const k = Math.min(1, MAX_SIDE / Math.max(bmp.width, bmp.height));
      const c = document.createElement("canvas");
      c.width = Math.round(bmp.width * k);
      c.height = Math.round(bmp.height * k);
      c.getContext("2d").drawImage(bmp, 0, 0, c.width, c.height);
      if (bmp.close) bmp.close();
      return (await new Promise((done) => c.toBlob(done, "image/jpeg", 0.85))) || file;
    } catch (e) {
      return file; // browser can't decode it here; let the server judge the original
    }
  }

  async function send(box, url, body) {
    box.classList.add("busy");
    try {
      const r = await fetch(url, { method: "POST", body, credentials: "same-origin" });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(data.error || "Upload failed");
      const img = box.querySelector("img");
      if (data.url) img.src = data.url;
      else img.removeAttribute("src");
      box.classList.toggle("has", !!data.url);
      RM.toast(data.url ? "Photo saved" : "Photo removed", "ok");
    } catch (e) {
      RM.toast(e.message === "Failed to fetch" ? "Can't reach the server" : e.message, "err");
    } finally {
      box.classList.remove("busy");
    }
  }

  const base = (box) => `/api/entry/${box.dataset.entry}/photo/${box.dataset.kind}`;

  document.addEventListener("change", async (ev) => {
    const input = ev.target;
    const box = input.closest && input.closest(".ci-photo");
    if (!box || !input.files || !input.files[0]) return;
    const body = new FormData();
    body.append("photo", await shrink(input.files[0]), "photo.jpg");
    input.value = "";
    send(box, base(box), body);
  });

  document.addEventListener("click", (ev) => {
    const btn = ev.target.closest && ev.target.closest(".ci-photo-del");
    if (!btn) return;
    const box = btn.closest(".ci-photo");
    if (confirm("Remove this photo?")) send(box, base(box) + "/delete");
  });
})();

// Step-by-step check-in: one form, shown a step at a time. Nothing is saved
// until the inspection verdict, so Back is always safe (photos save at once).
(function () {
  const form = document.querySelector(".ci-wiz");
  if (!form) return;
  const steps = [...form.querySelectorAll(".ci-step")];
  const dots = [...form.querySelectorAll(".ci-steps li")];
  let at = 0;

  function show(i) {
    at = Math.max(0, Math.min(steps.length - 1, i));
    steps.forEach((s, k) => (s.hidden = k !== at));
    dots.forEach((d, k) => {
      d.classList.toggle("now", k === at);
      d.classList.toggle("past", k < at);
    });
    window.scrollTo(0, 0);
    const weight = steps[at].querySelector("[name=weight_oz]");
    if (weight) weight.focus(); // the scale is out; save a tap
  }

  function valid() {
    const w = steps[at].querySelector("[name=weight_oz]");
    const v = w ? w.value.trim() : "";
    if (v && !(Number(v) > 0)) {
      RM.toast(`${v} is not a weight`, "err");
      w.focus();
      return false;
    }
    return true;
  }

  const next = () => valid() && show(at + 1);
  const save = () => htmx.trigger(form, "submit");

  form.addEventListener("click", (ev) => {
    const b = ev.target.closest("button");
    if (!b) return;
    if (b.hasAttribute("data-next")) next();
    else if (b.hasAttribute("data-back")) show(at - 1);
    else if (b.dataset.verdict === "passed") {
      form.elements.inspection_status.value = "passed";
      form.elements.inspection_notes.value = "";
      save();
    } else if (b.dataset.verdict === "failed") {
      form.elements.inspection_status.value = "failed";
      const fail = form.querySelector(".ci-fail");
      fail.hidden = false;
      fail.querySelector("input").focus();
    } else if (b.hasAttribute("data-undo") && confirm("Mark this scout as not checked in?")) {
      form.elements.checked_in.value = "off";
      save();
    }
  });

  // Enter in a field moves on rather than saving half-way through
  form.addEventListener("keydown", (ev) => {
    if (ev.key !== "Enter" || ev.target.tagName !== "INPUT") return;
    if (ev.target.name === "inspection_notes") return; // last step: Enter checks in as failed
    ev.preventDefault();
    next();
  });

  show(0);
})();
