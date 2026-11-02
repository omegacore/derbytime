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
