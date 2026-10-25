(function (root) {
  "use strict";
  function create({doc, settingsPanel, url, onLayout, onPaint}) {
    const label = doc.createElement("label"), toggle = doc.createElement("input");
    toggle.type = "checkbox";
    label.style.cssText = "display:flex;align-items:center;gap:8px";
    label.append(toggle, doc.createTextNode("显示画面预览图"));
    const status = doc.createElement("small"), retry = doc.createElement("button");
    status.setAttribute("role", "status");
    status.className = "settings-panel-hint";
    retry.type = "button"; retry.textContent = "生成 / 重试"; retry.hidden = true;
    settingsPanel.append(label, status, retry);
    let manifest = null, closed = false, timer = 0, enabled = false, chosen = false, version = 0, busy = false;
    let active = 0, imageEpoch = 0, generateAgain = false;
    let abort = new AbortController(), requestEpoch = 0;
    const images = new Map(), queue = new Map(), pending = new Set(), failed = new Set();
    let preference = `substar.editor.thumbnails:${url}`;
    try { const saved = localStorage.getItem(preference); chosen = saved !== null; enabled = saved === "true"; } catch (_) {}
    toggle.checked = enabled;
    function show(next) {
      if (enabled === next) return;
      enabled = next; toggle.checked = next; onLayout();
    }
    function paintStatus() {
      const state = manifest?.state || "idle";
      status.textContent = ({idle:"尚未生成画面预览图", queued:"画面预览图等待生成", generating:`正在生成画面预览图 ${Math.round((manifest?.progress || 0) * 100)}%`,
        ready:`画面预览图已就绪 · 每 ${manifest?.interval || .5} 秒一帧`, unavailable:"此素材没有视频画面", error:manifest?.message || "画面预览图读取失败"})[state];
      retry.hidden = !["idle", "error"].includes(state);
      toggle.disabled = state === "unavailable";
    }
    async function refresh(generate = false) {
      if (closed) return;
      if (busy) { generateAgain ||= generate; return; }
      busy = true; clearTimeout(timer);
      const epoch = requestEpoch;
      try {
        const response = await fetch(url, {method:generate ? "POST" : "GET", signal:abort.signal, cache:"no-store"});
        if (!response.ok) throw new Error("Thumbnail request failed");
        const next = await response.json();
        if (closed || epoch !== requestEpoch) return;
        if (manifest?.revision !== next.revision) { drop(); version++; }
        manifest = next;
        if (!chosen && manifest.requested && manifest.state !== "unavailable") show(true);
        if (manifest.state === "unavailable") show(false);
        paintStatus(); onPaint();
        if (["queued", "generating"].includes(manifest.state)) timer = setTimeout(() => refresh(), 2000);
      } catch (error) {
        if (closed || epoch !== requestEpoch || error.name === "AbortError") return;
        status.textContent = "画面预览图读取失败"; retry.hidden = false;
      } finally {
        if (epoch === requestEpoch) {
          busy = false;
          if (generateAgain && !closed) { generateAgain = false; void refresh(true); }
        }
      }
    }
    toggle.onchange = () => {
      chosen = true; show(toggle.checked);
      try { localStorage.setItem(preference, String(enabled)); } catch (_) {}
      if (enabled && (!manifest || ["idle", "error"].includes(manifest.state))) refresh(true);
    };
    retry.onclick = () => { chosen = true; show(true); refresh(true); };
    function drop() {
      imageEpoch++; active = 0; pending.clear();
      for (const image of images.values()) { image.onload = image.onerror = null; image.removeAttribute("src"); }
      images.clear(); queue.clear(); failed.clear();
    }
    function drain() {
      while (!closed && enabled && active < 2 && queue.size) {
        const [key, src] = queue.entries().next().value; queue.delete(key);
        if (images.has(key) || pending.has(key)) continue;
        active++; pending.add(key);
        const epoch = imageEpoch;
        const image = doc.createElement("img"); images.set(key, image);
        const done = () => {
          if (epoch !== imageEpoch) return;
          active--; pending.delete(key);
          if (closed) return;
          version++; onPaint(); drain();
        };
        image.onload = done;
        image.onerror = () => { failed.add(key); images.delete(key); done(); };
        image.src = src;
        while (images.size > 10) {
          const oldest = [...images.keys()].find(value => !pending.has(value));
          if (oldest === undefined) break;
          const old = images.get(oldest); images.delete(oldest); old.onload = old.onerror = null; old.removeAttribute("src");
        }
      }
    }
    function draw(canvas, start, span) {
      if (!enabled || closed) return;
      const width = canvas.clientWidth, dpr = Math.min(2, doc.defaultView.devicePixelRatio || 1);
      if (!width) return;
      const key = `${width}|${dpr}|${start}|${span}|${version}`;
      if (canvas.dataset.paintKey === key) return;
      canvas.dataset.paintKey = key;
      canvas.width = Math.round(width * dpr); canvas.height = 48 * dpr;
      const ctx = canvas.getContext("2d"); ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.fillStyle = "#12171e"; ctx.fillRect(0, 0, width, 48);
      if (manifest?.state !== "ready") {
        ctx.fillStyle = "#a5b0b8"; ctx.font = "12px sans-serif"; ctx.fillText(status.textContent, 10, 28); return;
      }
      const tileWidth = 48 * 160 / 90;
      for (let x = 0; x < width; x += tileWidth) {
        const time = start + x / width * span;
        if (time >= manifest.duration) break;
        const index = Math.min(manifest.count - 1, Math.floor(time / manifest.interval));
        const sprite = Math.floor(index / 64) + 1, cell = index % 64;
        if (sprite > manifest.sprites) continue;
        const asset = `sprite-${String(sprite).padStart(5, "0")}.jpg`;
        const src = `${url}/${manifest.revision}/${asset}`;
        const image = images.get(src);
        if (image?.complete && image.naturalWidth) {
          images.delete(src); images.set(src, image);
          ctx.drawImage(image, cell % 8 * 160, Math.floor(cell / 8) * 90, 160, 90, x, 0, tileWidth, 48);
        } else if (!pending.has(src) && !failed.has(src) && queue.size < 32) queue.set(src, src);
      }
      drain();
    }
    if (url && !url.includes("/projects//")) void refresh();
    return {get enabled() { return enabled; }, draw,
      setUrl(next) {
        if (!next || next === url) return;
        requestEpoch++; abort.abort(); abort = new AbortController(); clearTimeout(timer);
        drop(); version++; busy = false; generateAgain = false; manifest = null; url = next;
        preference = `substar.editor.thumbnails:${url}`; chosen = false;
        let nextEnabled = false;
        try { const saved = localStorage.getItem(preference); chosen = saved !== null; nextEnabled = saved === "true"; } catch (_) {}
        show(nextEnabled); paintStatus(); void refresh();
      },
      close() { closed = true; clearTimeout(timer); abort.abort(); drop(); label.remove(); status.remove(); retry.remove(); }};
  }
  root.EditorThumbnails = {create};
})(globalThis);
