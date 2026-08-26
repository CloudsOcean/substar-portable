(() => {
  const defaults = {
    appearance_mode: "dark",
    accent_color: "purple",
    surface_style: "standard",
    ui_density: "comfortable",
    motion_level: "full",
    font_scale: "standard",
  };
  const key = "substar.personalization.v1";
  const backgroundPages = ["split", "editor", "glossary", "settings"];
  const backgroundLabels = { split: "切分", editor: "编辑", glossary: "词库", settings: "设置" };
  const backgroundDbName = "substar.personalization.assets.v1";
  const backgroundStore = "backgrounds";
  let revision = 0;
  let activeBackgroundUrl = "";

  function currentPage() {
    const path = location.pathname.toLowerCase();
    if (path.startsWith("/editor") || path.startsWith("/relay")) return "editor";
    if (path.startsWith("/glossary")) return "glossary";
    if (path.startsWith("/settings")) return "settings";
    return "split";
  }

  function openBackgroundDb() {
    return new Promise((resolve, reject) => {
      const request = indexedDB.open(backgroundDbName, 1);
      request.onupgradeneeded = () => {
        const db = request.result;
        if (!db.objectStoreNames.contains(backgroundStore)) db.createObjectStore(backgroundStore, { keyPath: "page" });
      };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
  }

  async function backgroundTransaction(mode, action) {
    const db = await openBackgroundDb();
    try {
      return await new Promise((resolve, reject) => {
        const transaction = db.transaction(backgroundStore, mode);
        const store = transaction.objectStore(backgroundStore);
        let result;
        try { result = action(store); }
        catch (error) { reject(error); return; }
        transaction.oncomplete = () => resolve(result);
        transaction.onerror = () => reject(transaction.error);
        transaction.onabort = () => reject(transaction.error || new Error("背景图存储已取消"));
      });
    } finally { db.close(); }
  }

  async function getBackground(page) {
    const db = await openBackgroundDb();
    try {
      return await new Promise((resolve, reject) => {
        const request = db.transaction(backgroundStore, "readonly").objectStore(backgroundStore).get(page);
        request.onsuccess = () => resolve(request.result || null);
        request.onerror = () => reject(request.error);
      });
    } finally { db.close(); }
  }

  async function listBackgrounds() {
    const records = await Promise.all(backgroundPages.map(getBackground));
    return Object.fromEntries(backgroundPages.map((page, index) => [page, records[index]]));
  }

  async function setBackground(page, file) {
    if (!backgroundPages.includes(page)) throw new Error("未知页面");
    if (!file || typeof file.arrayBuffer !== "function" || !String(file.type || "").startsWith("image/")) throw new Error("请选择图片文件");
    if (file.size > 30 * 1024 * 1024) throw new Error("背景图不能超过 30 MB");
    const record = { page, blob: file, name: file.name || "背景图", type: file.type, size: file.size, updatedAt: Date.now() };
    await backgroundTransaction("readwrite", (store) => store.put(record));
    if (page === currentPage()) await applyBackground(page);
    return record;
  }

  async function removeBackground(page) {
    await backgroundTransaction("readwrite", (store) => store.delete(page));
    if (page === currentPage()) await applyBackground(page);
  }

  async function applyBackgroundToAll(sourcePage) {
    const source = await getBackground(sourcePage);
    if (!source?.blob) throw new Error("请先为当前页面选择背景图");
    await backgroundTransaction("readwrite", (store) => {
      for (const page of backgroundPages) store.put({ ...source, page, updatedAt: Date.now() });
    });
    await applyBackground(currentPage());
  }

  async function applyBackground(page = currentPage()) {
    const root = document.documentElement;
    try {
      const record = await getBackground(page);
      if (activeBackgroundUrl) URL.revokeObjectURL(activeBackgroundUrl);
      activeBackgroundUrl = record?.blob ? URL.createObjectURL(record.blob) : "";
      if (activeBackgroundUrl) {
        root.style.setProperty("--theme-background-image", `url("${activeBackgroundUrl}")`);
        root.dataset.hasBackground = "true";
      } else {
        root.style.removeProperty("--theme-background-image");
        root.dataset.hasBackground = "false";
      }
      return record;
    } catch (_) {
      root.style.removeProperty("--theme-background-image");
      root.dataset.hasBackground = "false";
      return null;
    }
  }

  function read() {
    try {
      const value = { ...defaults, ...JSON.parse(localStorage.getItem(key) || "{}") };
      if (!['dark', 'light'].includes(value.appearance_mode)) value.appearance_mode = "dark";
      return value;
    }
    catch (_) { return { ...defaults }; }
  }

  function apply(value = read()) {
    const root = document.documentElement;
    root.dataset.theme = value.appearance_mode === "light" ? "light" : "dark";
    root.dataset.accent = value.accent_color || defaults.accent_color;
    root.dataset.surface = value.surface_style || defaults.surface_style;
    root.dataset.density = value.ui_density || defaults.ui_density;
    root.dataset.motion = value.motion_level || defaults.motion_level;
    root.dataset.fontScale = value.font_scale || defaults.font_scale;
    // Canvas-based views (notably the editor timeline) cannot inherit CSS
    // colors automatically.  Give them one stable signal whenever the
    // semantic theme changes so they can redraw from the same token contract.
    root.dispatchEvent(new CustomEvent("substar:themechange", { detail: { ...value } }));
    return value;
  }

  function save(value) {
    revision += 1;
    const next = { ...read(), ...value };
    if (!['dark', 'light'].includes(next.appearance_mode)) next.appearance_mode = "dark";
    localStorage.setItem(key, JSON.stringify(next));
    apply(next);
    return next;
  }

  function preview(value) {
    revision += 1;
    return apply({ ...read(), ...value });
  }

  window.SubstarTheme = { defaults, read, save, apply, preview };
  window.SubstarBackgrounds = {
    pages: backgroundPages,
    labels: backgroundLabels,
    currentPage,
    get: getBackground,
    list: listBackgrounds,
    set: setBackground,
    remove: removeBackground,
    apply: applyBackground,
    applyToAll: applyBackgroundToAll,
  };
  apply();
  applyBackground();
})();
