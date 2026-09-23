// Shared by every page: API helpers, the step bar, background-job polling, face crops.
const FA = (() => {
  const el = (t, c, x) => { const e = document.createElement(t); if (c) e.className = c; if (x != null) e.textContent = x; return e; };

  async function get(path) {
    const r = await fetch(path);
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || r.statusText);
    return d;
  }
  // Every POST carries X-Face-Audit: the server rejects POSTs without it (cross-site protection).
  async function post(path, body) {
    const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json", "X-Face-Audit": "1" },
                                  body: JSON.stringify(body ?? {}) });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || r.statusText);
    return d;
  }

  const STEPS = [
    ["connect", "Connect", s => s.connected, () => true],
    ["data", "Data", s => !!s.data, s => s.connected],
    ["references", "References", s => s.reviewedPeople > 0 || !!s.flags, s => !!s.references],
    ["flags", "Flags", s => !!s.flags && !s.flagsStale, s => !!s.flags],
    ["apply", "Apply", s => s.applied > 0, s => s.decisions > 0 && s.connected],
  ];

  async function steps(active) {
    const s = await get("/audit/status");
    const nav = document.getElementById("steps");
    nav.replaceChildren();
    STEPS.forEach(([key, label, done, open], i) => {
      if (i) nav.append(el("span", "arrow", "›"));
      const warn = (key === "flags" && s.flagsStale) || (key === "references" && s.referencesStale);
      const a = el("a", [key === active ? "on" : "", done(s) ? "done" : "", warn ? "warn" : "",
                         open(s) || key === active ? "" : "locked"].join(" "));
      a.href = "/" + key;
      a.append(el("span", "dot", done(s) && !warn ? "✓" : warn ? "!" : String(i + 1)), el("span", null, label));
      nav.append(a);
    });
    nav.append(el("span", "sp"));
    const j = s.job && s.job.state === "running" ? `⏳ ${s.job.kind} running…` : "";
    nav.append(el("span", "job", j || s.workdir));
    return s;
  }

  // Start a background job and poll it; onUpdate(snapshot) on every tick. Resolves with the final snapshot.
  async function runJob(kind, body, onUpdate) {
    await post(`/audit/jobs/${kind}`, body);
    return follow(onUpdate);
  }
  async function follow(onUpdate) {
    for (;;) {
      const j = await get("/audit/job");
      onUpdate && onUpdate(j);
      if (!j || j.state !== "running") return j;
      await new Promise(r => setTimeout(r, 600));
    }
  }

  // A job panel: progress bar + log tail. Returns an update(snapshot) function.
  function jobPanel(container) {
    const bar = el("div", "progress"), fill = el("i"), log = el("div", "log");
    bar.append(fill); container.replaceChildren(bar, log);
    return j => {
      if (!j) return;
      const p = j.progress;
      fill.style.width = p ? `${Math.round(100 * p[0] / Math.max(1, p[1]))}%` : (j.state === "running" ? "100%" : "0");
      fill.style.opacity = p || j.state !== "running" ? 1 : .35;
      log.textContent = j.log.join("\n") + (j.error ? `\n⚠ ${j.error}` : "");
      log.scrollTop = log.scrollHeight;
    };
  }

  // Face crop from Immich's thumbnail (small faces use the larger preview).
  function crop(g, D) {
    const [fid, aid, W, H, x1, y1, x2, y2] = g;
    const box = el("div", "f"); box.style.width = box.style.height = D + "px";
    const side = Math.max(x2 - x1, y2 - y1) * 1.5, cx = (x1 + x2) / 2, cy = (y1 + y2) / 2, s = D / side;
    const img = new Image(); img.loading = "lazy";
    img.src = `/api/assets/${aid}/thumbnail?size=${(x2 - x1) / W * 250 * W / Math.max(W, H) < D * 0.6 ? "preview" : "thumbnail"}`;
    Object.assign(img.style, { width: W * s + "px", height: H * s + "px", left: -(cx - side / 2) * s + "px", top: -(cy - side / 2) * s + "px" });
    box.append(img);
    return box;
  }

  return { el, get, post, steps, runJob, follow, jobPanel, crop };
})();
