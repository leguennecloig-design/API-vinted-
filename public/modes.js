/* =====================================================================================
   Modes d'exploration : 📡 Radar (veille continue toutes catégories), 🔎 Recherche instantanée,
   🎯 Méthode Evan. Tout est orchestré par la page (le serveur ne fait qu'une page Vinted par appel),
   les résultats sont gardés dans le navigateur (IndexedDB).
   ===================================================================================== */
"use strict";

const STATUS_OPTS = [[6, "Neuf avec étiquette"], [1, "Neuf sans étiquette"], [2, "Très bon état"], [3, "Bon état"], [4, "Satisfaisant"]];
const sleep = ms => new Promise(r => setTimeout(r, ms));
const fmtAge = m => m == null ? "–" : m < 60 ? `${Math.round(m)} min` : m < 1440 ? `${(m / 60).toFixed(1).replace(".", ",")} h` : `${(m / 1440).toFixed(1).replace(".", ",")} j`;

/* ---------- client d'exploration : cookies, repères de l'horloge des IDs, pauses, anti-blocage ---------- */
const XP = {
  cookies: null, anchors: [], loaded: false, demo: false,
  async init() {
    if (this.loaded) return;
    this.anchors = (await IDB.get("idclock").catch(() => null)) || [];
    this.loaded = true;
  },
  async keepAnchor(a) {
    if (!a || !a[1]) return;
    const last = this.anchors[this.anchors.length - 1];
    if (last && (a[1] <= last[1] || a[0] - last[0] < 300)) return;
    this.anchors.push(a); this.anchors = this.anchors.slice(-2000);
    await IDB.set("idclock", this.anchors).catch(() => {});
  },
  spaced() {   // ~120 repères répartis sur toute la plage connue (envoyés au serveur)
    const a = this.anchors; if (a.length <= 120) return a;
    const step = a.length / 120; return Array.from({length: 120}, (_, k) => a[Math.floor(k * step)]).concat([a[a.length - 1]]);
  },
  backoff: 0,
  async call(path, body) {
    await this.init();
    for (let attempt = 0; attempt < 4; attempt++) {
      let r;
      try { r = await serverApi(path, {method: "POST", body: JSON.stringify({...body, cookies: this.cookies, anchors: this.spaced(), demo: this.demo})}); }
      catch (e) { r = {status: "error", message: e.message}; }
      if (r.cookies) this.cookies = r.cookies;
      if (r.anchor) await this.keepAnchor(r.anchor);
      if (r.status === "rate_limited") {
        const wait = [30, 60, 120, 240][Math.min(this.backoff++, 3)];
        this.onStatus?.(`Vinted limite les requêtes : pause ${wait} s`, "warn"); this.cookies = null;
        await sleep(wait * 1000); continue;
      }
      if (r.status === "error" && attempt < 2) { this.onStatus?.(`Erreur : ${r.message} — nouvel essai`, "error"); await sleep(5000); continue; }
      this.backoff = 0;
      return r;
    }
    return {status: "error", message: "Échec après plusieurs essais", items: []};
  },
  page(filters, order, page) { return this.call("/api/explore", {filters, order, page}); },
  brand(b) { return this.call("/api/brand-info", b); },
};

/* ---------- sélecteur de catégories réutilisable ---------- */
function catPicker(el, initial = new Map(), {placeholder = "Rechercher une catégorie…"} = {}) {
  const sel = new Map(initial), open = new Set();
  el.innerHTML = `<div class="catpick"><div class="chips cs"></div><input type="text" class="cq" placeholder="${placeholder}" autocomplete="off"><div class="cattree ct"></div></div>`;
  const $c = s => el.querySelector(s);
  const drawSel = () => {
    $c(".cs").innerHTML = [...sel].map(([id, p]) => `<span class="chip">${esc(shortCat(p))} <a href="#" data-x="${id}">✕</a></span>`).join("") || `<span class="chip gray">Toutes les catégories</span>`;
    $c(".cs").querySelectorAll("[data-x]").forEach(a => a.onclick = e => { e.preventDefault(); sel.delete(+a.dataset.x); drawSel(); drawTree(); });
  };
  const row = (n, path, d) => {
    const p = [...path, n.title].join(" › "), has = n.children.length;
    return `<div class="catrow" style="padding-left:${8 + d * 18}px">${has ? `<button type="button" class="tw" data-o="${n.id}">${open.has(n.id) ? "▾" : "▸"}</button>` : `<span class="tw"></span>`}
      <label><input type="checkbox" data-c="${n.id}" data-p="${esc(p)}" ${sel.has(n.id) ? "checked" : ""}> ${esc(n.title)}</label></div>`
      + (has && open.has(n.id) ? n.children.map(c => row(c, [...path, n.title], d + 1)).join("") : "");
  };
  const drawTree = () => {
    const q = norm($c(".cq").value.trim()), box = $c(".ct");
    if (!CATTREE?.length) { box.innerHTML = `<div class="muted small" style="padding:8px">Catégories indisponibles.</div>`; return; }
    box.innerHTML = q.length >= 2
      ? CATPATHS.filter(c => norm(c.path).includes(q)).slice(0, 80).map(c => `<div class="catrow"><label><input type="checkbox" data-c="${c.id}" data-p="${esc(c.path)}" ${sel.has(c.id) ? "checked" : ""}> ${esc(c.path)}</label></div>`).join("")
      : CATTREE.map(n => row(n, [], 0)).join("");
    box.querySelectorAll("[data-o]").forEach(b => b.onclick = () => { const id = +b.dataset.o; open.has(id) ? open.delete(id) : open.add(id); drawTree(); });
    box.querySelectorAll("[data-c]").forEach(cb => cb.onchange = () => { cb.checked ? sel.set(+cb.dataset.c, cb.dataset.p) : sel.delete(+cb.dataset.c); drawSel(); });
  };
  drawSel(); loadCats().then(drawTree);
  let t; $c(".cq").addEventListener("input", () => { clearTimeout(t); t = setTimeout(drawTree, 150); });
  return {get: () => sel};
}
const rootCats = () => (CATTREE || []).map(n => [n.id, n.title]);
function statusPicker(el, initial = [2, 3]) {
  el.innerHTML = `<div class="chips">${STATUS_OPTS.map(([id, t]) => `<label class="chip ${initial.includes(id) ? "" : "gray"}" style="cursor:pointer"><input type="checkbox" value="${id}" ${initial.includes(id) ? "checked" : ""} style="display:none"> ${t}</label>`).join("")}</div>`;
  el.querySelectorAll("label").forEach(l => l.onclick = e => { e.preventDefault(); const cb = l.querySelector("input"); cb.checked = !cb.checked; l.classList.toggle("gray", !cb.checked); });
  return {get: () => [...el.querySelectorAll("input:checked")].map(i => +i.value)};
}
function itemsTable(id, rows, extraCols = []) {
  const cols = [
    {key: "photo", label: "", nosort: true, fmt: i => i.photo_url ? `<img loading="lazy" referrerpolicy="no-referrer" src="${esc(i.photo_url)}" alt="">` : ""},
    {key: "brand", label: "Marque", fmt: i => `<b>${esc(i.brand)}</b>`},
    ...extraCols,
    {key: "price", label: "Prix", num: true, fmt: i => eur(i.price)},
    {key: "title", label: "Titre", cls: "title", fmt: i => `<a href="${esc(i.url)}" target="_blank" rel="noopener">${esc(i.title || "Annonce")}</a>`},
    {key: "cat", label: "Catégorie", fmt: i => esc(shortCat(i.cat || ""))},
  ];
  return table(id, cols, rows, extraCols[0]?.key || "favs");
}
const progressBar = (done, total, text) => `<div class="phase"><b>${text}</b><span class="muted small">${done} / ${total}</span></div>
  <div class="track"><div class="seg" style="width:100%"><div class="fill" style="width:${total ? 100 * done / total : 0}%"></div></div></div>`;

/* =====================================================================================
   🔎 RECHERCHE INSTANTANÉE : ex. ≥ 40 favoris, postées il y a 1 à 2 jours
   ===================================================================================== */
const SEARCH = {running: false, stop: false, results: [], seen: new Set(), log: ""};
async function viewSearch(token) {
  await loadCats();
  if (!SEARCH.running && !SEARCH.results.length) { const last = await IDB.get("search:last").catch(() => null); if (last) SEARCH.results = last; }
  const f = (() => { try { return JSON.parse(localStorage.getItem("vnw-search")) || {}; } catch { return {}; } })();
  $("#app").innerHTML = `
  <form class="card" id="sf">
    <h2>🔎 Recherche instantanée</h2>
    <p class="muted small" style="margin-top:-8px">Les annonces qui ont déjà beaucoup de favoris pour leur âge. Vide = toutes les grandes catégories.</p>
    <label>Catégories</label><div id="scat"></div>
    <div class="fields" style="margin-top:12px">
      <div><label>Favoris minimum</label><input type="number" id="sfav" min="0" value="${f.fav ?? 40}"></div>
      <div><label>Postée il y a (jours) — de</label><input type="number" id="sa0" min="0" step="0.5" value="${f.a0 ?? 1}"></div>
      <div><label>à</label><input type="number" id="sa1" min="0" step="0.5" value="${f.a1 ?? 2}"></div>
    </div>
    <details style="margin-top:12px"><summary class="small" style="cursor:pointer;font-weight:600">Réglages avancés</summary>
      <div class="fields" style="margin-top:10px">
        <div><label>Prix min (€)</label><input type="number" id="spf" min="0" value="${f.pf ?? ""}"></div>
        <div><label>Prix max (€)</label><input type="number" id="spt" min="0" value="${f.pt ?? ""}"></div>
        <div><label>Pages par catégorie</label><input type="number" id="spg" min="1" max="10" value="${f.pg ?? 5}"></div>
      </div>
      <label style="margin-top:10px">État</label><div id="sst"></div>
      <label class="check" style="margin-top:10px"><input type="checkbox" id="sband" ${f.band ? "checked" : ""}><span>Élargir avec 3 tranches de prix (×3 résultats parcourus)</span></label>
      <label class="check" style="margin-top:6px"><input type="checkbox" id="sdemo" ${f.demo ? "checked" : ""}><span>Démo (données simulées)</span></label>
    </details>
    <div class="row" style="margin-top:14px"><span class="spacer"></span><button class="btn" id="sgo">${SEARCH.running ? "■ Arrêter" : "🔎 Lancer la recherche"}</button></div>
  </form>
  <div class="card" id="sres"></div>`;
  const picker = catPicker($("#scat"), new Map(Object.entries(f.cats || {}).map(([k, v]) => [+k, v])), {placeholder: "Vide = toutes les catégories principales"});
  const st = statusPicker($("#sst"), f.st || []);
  const draw = () => {
    if (token !== renderToken) return;
    const r = [...SEARCH.results].sort((a, b) => b.favs - a.favs);
    const bl = [...groupBrands(r).values()].map(({name, items}) => ({brand: name, n: items.length, med: median(items.map(i => i.favs)), max: Math.max(...items.map(i => i.favs))}))
      .sort((a, b) => b.n - a.n || b.med - a.med).slice(0, 15);
    $("#sres").innerHTML = `${SEARCH.running ? progressBar(SEARCH.done, SEARCH.total, SEARCH.log) : `<h2>Résultats ${r.length ? `(${r.length})` : ""}</h2>`}
      ${r.length ? `<div class="row" style="margin:10px 0"><span class="muted small">Marques les plus présentes :</span>${bl.map(b => `<span class="chip">${esc(b.brand)} · ${b.n} · méd. ${nf(b.med)} fav</span>`).join("")}
        <span class="spacer"></span><button class="btn ghost sm" id="scsv">⇩ CSV</button></div>
        <div id="stab"></div>` : `<div class="empty">${SEARCH.running ? "Recherche en cours…" : "Lance une recherche."}</div>`}`;
    if (r.length) {
      const rows = r.map(i => ({...i, per_day: i.favs / Math.max(i.age_min / 1440, 0.05)}));
      const t = () => { $("#stab").innerHTML = itemsTable("search", rows, [
        {key: "favs", label: "Favoris", num: true, fmt: i => `<b>${i.favs}</b>`},
        {key: "age_min", label: "Âge", num: true, fmt: i => fmtAge(i.age_min)},
        {key: "per_day", label: "Fav./jour", num: true, fmt: i => nf(i.per_day, 1)}]); bindTables($("#stab"), t); };
      t();
      $("#scsv").onclick = () => downloadCsv([["id", "marque", "titre", "prix", "favoris", "âge (jours)", "favoris/jour", "catégorie", "lien"],
        ...rows.map(i => [i.item_id, i.brand, i.title, csvNum(i.price), i.favs, csvNum(i.age_min / 1440, 2), csvNum(i.per_day, 1), i.cat, i.url])], "recherche_vinted.csv");
    }
  };
  XP.onStatus = (m) => { SEARCH.log = m; draw(); };
  draw();
  $("#sf").onsubmit = async e => {
    e.preventDefault();
    if (SEARCH.running) { SEARCH.stop = true; return; }
    const num = id => $(id).value === "" ? null : Number($(id).value);
    const cfg = {cats: Object.fromEntries(picker.get()), fav: num("#sfav") ?? 40, a0: num("#sa0") ?? 0, a1: num("#sa1") ?? 7, pf: num("#spf"), pt: num("#spt"),
                 pg: Math.min(10, Math.max(1, num("#spg") ?? 5)), st: st.get(), band: $("#sband").checked, demo: $("#sdemo").checked};
    try { localStorage.setItem("vnw-search", JSON.stringify(cfg)); } catch {}
    XP.demo = cfg.demo;
    const cats = picker.get().size ? [...picker.get()] : rootCats();
    let bands = [[cfg.pf, cfg.pt]];
    if (cfg.band) { const lo = cfg.pf ?? 0, hi = cfg.pt ?? 150, a = lo + (hi - lo) / 3, b = lo + 2 * (hi - lo) / 3;
      bands = [[cfg.pf, Math.round(a) - 0.01], [Math.round(a), Math.round(b) - 0.01], [Math.round(b), cfg.pt]]; }
    const jobs = []; for (const [cid, cpath] of cats) for (const [lo, hi] of bands) for (let p = 1; p <= cfg.pg; p++) jobs.push({cid, cpath, lo, hi, p});
    Object.assign(SEARCH, {running: true, stop: false, results: [], seen: new Set(), done: 0, total: jobs.length, log: "Recherche en cours"});
    $("#sgo").textContent = "■ Arrêter"; draw();
    const empty = new Map();
    for (const j of jobs) {
      if (SEARCH.stop) break;
      const key = `${j.cid}:${j.lo}:${j.hi}`;
      if ((empty.get(key) || 0) >= 99) { SEARCH.done++; continue; }
      SEARCH.log = `${shortCat(j.cpath)} · page ${j.p}${bands.length > 1 ? ` · ${j.lo ?? 0}–${j.hi ?? "∞"} €` : ""}`;
      const r = await XP.page({catalog_ids: [j.cid], price_from: j.lo, price_to: j.hi, status_ids: cfg.st}, "relevance", j.p);
      SEARCH.done++;
      if (!r.items?.length) empty.set(key, 99);   // plus de résultats pour cette recherche
      for (const it of r.items || []) {
        if (it.promoted || SEARCH.seen.has(it.item_id)) continue;
        SEARCH.seen.add(it.item_id);
        const days = it.age_min / 1440;
        if (it.favs >= cfg.fav && days >= cfg.a0 && days <= cfg.a1) SEARCH.results.push({...it, cat: j.cpath});
      }
      draw();
      await sleep(XP.demo ? 50 : 900 + Math.random() * 700);
    }
    SEARCH.running = false; SEARCH.log = ""; await IDB.set("search:last", SEARCH.results).catch(() => {});
    if (token === renderToken) { $("#sgo").textContent = "🔎 Lancer la recherche"; draw(); }
  };
}

/* =====================================================================================
   🎯 TROUVER DES NICHES (méthode Evan, version poussée)
   Demande → Flow rate → Concurrence → Win rate, mais en score 0-100 : une marque connue n'est pas
   éliminée si la demande est forte ; le flux (annonces publiées / jour) est mesuré pour chaque niche.
   ===================================================================================== */
const EVAN_DEF = {fav: 20, days: 7, pages: 5, maxNiches: 30, subcats: true, maxSub: 15, flow2: 250, margin: 3,
  ultra: "Stone Island, Moncler, C.P. Company, Levi's, Lacoste, Patagonia, Nike, Carhartt"};
const EVAN = {running: false, stop: false, cands: [], niches: [], log: "", done: 0, total: 0, sort: "score"};
function subcatsOf(id, path, max) {   // feuilles sous une catégorie (sinon ses enfants directs)
  let node = null; const find = (ns) => { for (const n of ns) { if (n.id === id) { node = n; return; } find(n.children); } }; find(CATTREE || []);
  if (!node || !node.children.length) return [[id, path]];
  const leaves = []; const walk = (n, p) => n.children.length ? n.children.forEach(c => walk(c, `${p} › ${c.title}`)) : leaves.push([n.id, p]);
  node.children.forEach(c => walk(c, `${path} › ${c.title}`));
  return leaves.length <= max ? leaves : node.children.slice(0, max).map(c => [c.id, `${path} › ${c.title}`]);
}
function nicheScore(n, cfg) {
  const i = n.info || {}, why = [], plus = [];
  // DEMANDE (40) : nombre d'articles très likés récents + favoris par jour
  // un seul article très liké ne suffit pas : le poids des favoris/jour grandit avec le nombre d'articles
  const D = Math.min(1, n.cands / 8) * 20 + Math.min(1, n.perDay / 25) * 20 * Math.min(1, n.cands / 3);
  if (D >= 28) plus.push("forte demande"); else if (D < 12) why.push("demande faible");
  // FLUX (25) : annonces publiées par jour dans la niche + volume dans la tranche de prix
  const flow = i.flow_per_day ?? 0, tot = i.niche_price_total ?? 0;
  const F = Math.min(1, flow / 20) * 15 + Math.min(1, tot / cfg.flow2) * 10;
  if (flow >= 10) plus.push(`${nf(flow, 0)} annonces/jour`); else if (flow < 2) why.push("peu de flux (élargis : autre catégorie proche, autre genre)");
  // COHÉRENCE DES PRIX / WIN RATE (20)
  const sp = i.price_band_prices?.spread;
  const P = sp == null ? 5 : Math.max(0, Math.min(1, (0.9 - sp) / 0.55)) * 20;
  if (sp != null && sp <= 0.4) plus.push("prix cohérents"); else if (sp != null && sp > 0.7) why.push("prix dispersés (cibler un modèle / mot-clé)");
  // CONCURRENCE (15) : revendeurs ; marque très connue = malus, sauf si la demande est forte
  let C = 15 * (1 - Math.min(1, (i.resellers ?? 0.1) / 0.5));
  if (n.ultra || n.mainstream) { C -= D >= 28 ? 3 : 10; (D >= 28 ? plus : why).push(D >= 28 ? "marque connue mais très demandée" : "marque très concurrentielle"); }
  if ((i.resellers ?? 0) > 0.35) why.push("beaucoup de revendeurs");
  const score = Math.round(Math.max(0, Math.min(100, D + F + P + C)));
  const verdict = tot < 30 ? "NO-GO" : score >= 65 ? "GO" : score >= 45 ? "À tester" : "NO-GO";
  if (tot < 30) why.unshift("presque rien à acheter");
  return {score, verdict, why: why.join(" · "), plus: plus.join(" · ")};
}
async function viewEvan(token) {
  await loadCats();
  if (!EVAN.running && !EVAN.niches.length) { const last = await IDB.get("evan:last").catch(() => null); if (last) Object.assign(EVAN, last); }
  const f = (() => { try { return {...EVAN_DEF, ...JSON.parse(localStorage.getItem("vnw-evan"))}; } catch { return {...EVAN_DEF}; } })();
  $("#app").innerHTML = `
  <form class="card" id="ef">
    <h2>🎯 Trouver des niches</h2>
    <p class="muted small" style="margin-top:-8px">Choisis une catégorie (même large, ex. « Chaussures femme ») : l'app passe toutes ses sous-catégories, garde les articles très likés et récents, puis note chaque niche marque × catégorie sur 100.</p>
    <label>Catégorie(s)</label><div id="ecat"></div>
    <div class="fields" style="margin-top:12px">
      <div><label>Prix min (€)</label><input type="number" id="epf" value="${f.pf ?? 20}"></div>
      <div><label>Prix max (€)</label><input type="number" id="ept" value="${f.pt ?? 150}"></div>
    </div>
    <label style="margin-top:12px">État</label><div id="est"></div>
    <details style="margin-top:12px"><summary class="small" style="cursor:pointer;font-weight:600">Réglages avancés</summary>
      <div class="fields" style="margin-top:10px">
        <div><label>Favoris min (demande)</label><input type="number" id="e_fav" value="${f.fav}"></div>
        <div><label>Âge max (jours)</label><input type="number" id="e_days" value="${f.days}"></div>
        <div><label>Pages par catégorie</label><input type="number" id="e_pages" min="1" max="10" value="${f.pages}"></div>
        <div><label>Niches mesurées max</label><input type="number" id="e_mn" min="5" max="80" value="${f.maxNiches}"></div>
        <div><label>Sous-catégories max</label><input type="number" id="e_ms" min="1" max="40" value="${f.maxSub}"></div>
        <div><label>Annonces min dans la tranche</label><input type="number" id="e_f2" value="${f.flow2}"></div>
        <div><label>Marge visée (×)</label><input type="number" id="e_mg" step="0.5" value="${f.margin}"></div>
      </div>
      <label class="check" style="margin-top:10px"><input type="checkbox" id="e_sub" ${f.subcats ? "checked" : ""}><span>Inclure toutes les sous-catégories</span></label>
      <label style="margin-top:10px">Marques très concurrentielles (malus, pas d'exclusion si forte demande)</label><input type="text" id="e_ultra" value="${esc(f.ultra)}">
      <label class="check" style="margin-top:10px"><input type="checkbox" id="edemo" ${f.demo ? "checked" : ""}><span>Démo (données simulées)</span></label>
    </details>
    <div class="row" style="margin-top:14px"><span class="spacer"></span><button class="btn" id="ego">${EVAN.running ? "■ Arrêter" : "🎯 Trouver des niches"}</button></div>
  </form>
  <div id="eres"></div>`;
  const picker = catPicker($("#ecat"), new Map(Object.entries(f.cats || {}).map(([k, v]) => [+k, v])), {placeholder: "ex : chaussures femme, sacs, montres…"});
  const st = statusPicker($("#est"), f.st || [2, 3]);
  const badge = n => `<span class="vbadge ${n.verdict === "GO" ? "go" : n.verdict === "À tester" ? "try" : n.verdict ? "no" : ""}">${n.verdict || "…"}${n.score != null ? ` · ${n.score}` : ""}</span>`;
  const draw = () => {
    if (token !== renderToken) return;
    const sorted = [...EVAN.niches].sort(EVAN.sort === "flow" ? (a, b) => (b.info?.flow_per_day ?? -1) - (a.info?.flow_per_day ?? -1)
      : EVAN.sort === "demand" ? (a, b) => b.perDay - a.perDay : (a, b) => (b.score ?? -1) - (a.score ?? -1));
    const go = EVAN.niches.filter(n => n.verdict === "GO").length, tr = EVAN.niches.filter(n => n.verdict === "À tester").length;
    $("#eres").innerHTML = `
    ${EVAN.running ? `<div class="card">${progressBar(EVAN.done, EVAN.total, EVAN.log)}</div>` : ""}
    ${EVAN.niches.length ? `<div class="card"><div class="row"><h2 style="margin:0">${EVAN.niches.length} niches — <span style="color:var(--ok)">${go} GO</span> · <span style="color:var(--warn)">${tr} à tester</span></h2><span class="spacer"></span>
        <span class="small muted">Trier :</span>${[["score", "Score"], ["flow", "Flux"], ["demand", "Demande"]].map(([k, l]) => `<button type="button" class="btn sm ${EVAN.sort === k ? "" : "ghost"}" data-sort="${k}">${l}</button>`).join("")}
        <button type="button" class="btn ghost sm" id="ecsv">⇩ CSV</button></div>
      <div class="hint" style="margin:6px 0 12px">Prix d'achat cible = prix médian ÷ ${f.margin} (marge ×${f.margin}). Vérifie ensuite à l'œil sur Vinted : fonds gris / dressings vitrines, articles de grossistes.</div>
      <div class="niches">${sorted.map(n => `
        <div class="niche ${n.verdict === "GO" ? "go" : n.verdict === "À tester" ? "try" : ""}">
          <div class="nh"><div><b>${esc(n.brand)}</b><div class="small muted">${esc(shortCat(n.cat))}</div></div>${badge(n)}</div>
          <div class="ng">
            <div><span>Demande</span><b>${n.cands} art.</b><small>${nf(n.perDay, 1)} fav/jour · max ${n.maxFav}</small></div>
            <div><span>Flux</span><b>${n.info ? `${nf(n.info.flow_per_day, 0)}/jour` : "…"}</b><small>${n.info ? `${n.info.niche_price_total >= 960 ? "960+" : nf(n.info.niche_price_total)} en vente` : ""}</small></div>
            <div><span>Prix revente</span><b>${n.info?.price_band_prices?.median != null ? eur(n.info.price_band_prices.median) : "…"}</b><small>${n.info?.price_band_prices?.p25 != null ? `${nf(n.info.price_band_prices.p25)}–${nf(n.info.price_band_prices.p75)} €` : ""}</small></div>
            <div><span>Achat cible</span><b style="color:var(--ok)">${n.info?.price_band_prices?.median != null ? "≤ " + eur(n.info.price_band_prices.median / f.margin) : "…"}</b><small>${n.info ? `revendeurs ${Math.round((n.info.resellers ?? 0) * 100)} %` : ""}</small></div>
          </div>
          ${n.plus ? `<div class="small" style="color:var(--ok)">✓ ${esc(n.plus)}</div>` : ""}${n.why ? `<div class="small" style="color:var(--err)">✗ ${esc(n.why)}</div>` : ""}
          <div class="row" style="margin-top:8px">${n.url ? `<a class="btn ghost sm" href="${esc(n.url)}" target="_blank" rel="noopener">Voir sur Vinted</a>` : ""}
            ${n.urlBuy ? `<a class="btn ghost sm" href="${esc(n.urlBuy)}" target="_blank" rel="noopener">Annonces au prix d'achat</a>` : ""}</div>
        </div>`).join("")}</div></div>` : ""}
    ${EVAN.cands.length ? `<div class="card"><details><summary style="cursor:pointer"><b>Articles très demandés trouvés (${EVAN.cands.length})</b></summary><div id="ecands" style="margin-top:10px"></div></details></div>` : ""}`;
    if (EVAN.cands.length) { const t = () => { $("#ecands").innerHTML = itemsTable("evan-cands", EVAN.cands, [
      {key: "favs", label: "Favoris", num: true, fmt: i => `<b>${i.favs}</b>`}, {key: "age_min", label: "Âge", num: true, fmt: i => fmtAge(i.age_min)}]); bindTables($("#ecands"), t); }; t(); }
    document.querySelectorAll("[data-sort]").forEach(b => b.onclick = () => { EVAN.sort = b.dataset.sort; draw(); });
    const c = $("#ecsv"); if (c) c.onclick = () => downloadCsv([["marque", "catégorie", "score", "verdict", "articles demandés", "favoris/jour", "favoris max", "flux (annonces/jour)", "annonces en vente (tranche)", "prix médian", "prix p25", "prix p75", "achat cible", "revendeurs", "points forts", "points faibles", "lien"],
      ...EVAN.niches.map(n => [n.brand, n.cat, n.score, n.verdict, n.cands, csvNum(n.perDay, 1), n.maxFav, csvNum(n.info?.flow_per_day, 1), n.info?.niche_price_total, csvNum(n.info?.price_band_prices?.median), csvNum(n.info?.price_band_prices?.p25), csvNum(n.info?.price_band_prices?.p75),
        n.info?.price_band_prices?.median != null ? csvNum(n.info.price_band_prices.median / f.margin) : "", csvNum(n.info?.resellers), n.plus, n.why, n.url])], "niches.csv");
  };
  XP.onStatus = m => { EVAN.log = m; draw(); };
  draw();
  $("#ef").onsubmit = async e => {
    e.preventDefault();
    if (EVAN.running) { EVAN.stop = true; return; }
    const num = id => $(id).value === "" ? null : Number($(id).value);
    const cfg = {cats: Object.fromEntries(picker.get()), pf: num("#epf"), pt: num("#ept"), st: st.get(), demo: $("#edemo").checked,
      fav: num("#e_fav") ?? 20, days: num("#e_days") ?? 7, pages: Math.min(10, num("#e_pages") ?? 5), maxNiches: Math.min(80, num("#e_mn") ?? 30),
      maxSub: num("#e_ms") ?? 15, flow2: num("#e_f2") ?? 250, margin: num("#e_mg") ?? 3, subcats: $("#e_sub").checked, ultra: $("#e_ultra").value};
    if (!picker.get().size) { alert("Choisis au moins une catégorie."); return; }
    try { localStorage.setItem("vnw-evan", JSON.stringify(cfg)); } catch {}
    XP.demo = cfg.demo;
    const ultra = new Set(cfg.ultra.split(",").map(x => brandKey(x.trim())).filter(Boolean));
    // catégories à parcourir (sous-catégories incluses)
    const cats = []; for (const [id, p] of picker.get()) for (const c of (cfg.subcats ? subcatsOf(id, p, cfg.maxSub) : [[id, p]])) if (!cats.some(x => x[0] === c[0])) cats.push(c);
    Object.assign(EVAN, {running: true, stop: false, cands: [], niches: [], done: 0, total: cats.length * cfg.pages + cfg.maxNiches, log: "Demande"});
    $("#ego").textContent = "■ Arrêter"; draw();
    // 1. DEMANDE : tri pertinence, articles très likés et récents
    const seen = new Set();
    for (const [cid, cpath] of cats) {
      for (let p = 1; p <= cfg.pages && !EVAN.stop; p++) {
        EVAN.log = `Demande — ${shortCat(cpath)}, page ${p}`; draw();
        const r = await XP.page({catalog_ids: [cid], price_from: cfg.pf, price_to: cfg.pt, status_ids: cfg.st}, "relevance", p);
        EVAN.done++;
        const its = r.items || [];
        for (const it of its) {
          if (it.promoted || seen.has(it.item_id)) continue; seen.add(it.item_id);
          if (it.favs > cfg.fav && it.age_min / 1440 < cfg.days && brandKey(it.brand) !== "(sans marque)") EVAN.cands.push({...it, cat: cpath, cid});
        }
        await sleep(XP.demo ? 30 : 800 + Math.random() * 600);
        if (its.length < 80) { EVAN.done += cfg.pages - p; break; }   // plus de résultats dans cette catégorie
      }
    }
    // niches = marque (regroupée) × catégorie, classées par demande
    const g = new Map();
    for (const c of EVAN.cands) { const k = `${brandKey(c.brand)}|${c.cid}`; if (!g.has(k)) g.set(k, {cat: c.cat, cid: c.cid, items: []}); g.get(k).items.push(c); }
    EVAN.niches = [...g.values()].map(n => {
      const names = {}; n.items.forEach(i => names[i.brand] = (names[i.brand] || 0) + 1);
      const perDay = median(n.items.map(i => i.favs / Math.max(i.age_min / 1440, 0.1)));
      return {...n, brand: Object.entries(names).sort((a, b) => b[1] - a[1])[0][0], cands: n.items.length, maxFav: Math.max(...n.items.map(i => i.favs)), perDay};
    }).sort((a, b) => (b.cands * 2 + b.perDay / 5) - (a.cands * 2 + a.perDay / 5)).slice(0, cfg.maxNiches);
    EVAN.total = EVAN.done + EVAN.niches.length; draw();
    // 2-4. FLUX, CONCURRENCE, PRIX : mesures puis score
    for (const n of EVAN.niches) {
      if (EVAN.stop) break;
      EVAN.log = `Mesure — ${n.brand} (${shortCat(n.cat)})`; draw();
      const info = await XP.brand({brand: n.brand, catalog_id: n.cid, price_from: cfg.pf, price_to: cfg.pt, status_ids: cfg.st});
      EVAN.done++;
      if (info.status !== "ok" || info.error) { n.verdict = "?"; n.why = info.error || info.message || "mesure impossible"; draw(); continue; }
      n.info = info; delete n.items;
      n.ultra = info.ultra_competitive || ultra.has(brandKey(info.brand_title || n.brand));
      n.mainstream = (info.brand_items || 0) > 1_000_000;
      Object.assign(n, nicheScore(n, cfg));
      const q = new URLSearchParams(); q.append("catalog[]", n.cid); q.append("brand_ids[]", info.brand_id);
      cfg.st.forEach(s => q.append("status_ids[]", s)); q.set("order", "newest_first");
      const q1 = new URLSearchParams(q); if (cfg.pf != null) q1.set("price_from", cfg.pf); if (cfg.pt != null) q1.set("price_to", cfg.pt);
      n.url = `https://www.vinted.fr/catalog?${q1}`;
      const med = info.price_band_prices?.median;
      if (med) { const q2 = new URLSearchParams(q); q2.set("price_to", Math.round(med / cfg.margin)); n.urlBuy = `https://www.vinted.fr/catalog?${q2}`; }
      draw();
      await sleep(XP.demo ? 30 : 900 + Math.random() * 600);
    }
    EVAN.running = false; await IDB.set("evan:last", {cands: EVAN.cands, niches: EVAN.niches.map(({items, ...n}) => n)}).catch(() => {});
    if (token === renderToken) { $("#ego").textContent = "🎯 Trouver des niches"; draw(); }
  };
}

/* =====================================================================================
   📡 RADAR : veille continue sur toutes les catégories -> favoris dans les 15 premières minutes,
   classement des marques sur la durée (heures / jours)
   ===================================================================================== */
const RADAR = {running: false, stop: false, data: null, log: "", status: "ok"};
function radarNew(cfg) {
  return {cfg, started: Date.now() / 1000, ticks: 0, requests: 0, items: {}, supply: {}, supplySeen: {}, rot: 0};
}
// Favoris à 15 min (W) : interpolation si deux relevés encadrent W, sinon projection prudente (puissance 0,75).
function fav15(obs, W) {
  const o = [...obs].sort((a, b) => a[0] - b[0]), t = W * 60;
  const bef = o.filter(x => x[0] <= t).pop(), aft = o.find(x => x[0] > t);
  if (bef && aft && aft[0] - bef[0] <= 900) return {v: Math.round(bef[1] + (aft[1] - bef[1]) * (t - bef[0]) / (aft[0] - bef[0])), exact: true};
  if (bef && t - bef[0] <= 60) return {v: bef[1], exact: true};
  if (aft && aft[0] - t <= 60) return {v: aft[1], exact: true};
  const near = bef && bef[0] >= 300 ? bef : aft && aft[0] <= 2 * t ? aft : null;
  if (!near) return {v: null, exact: false};
  let v = near[1] * (t / near[0]) ** 0.75;          // projection « puissance 0,75 » (les favoris ralentissent)
  if (near === bef) {                               // … bornée par le rythme récent (pente des derniers relevés)
    const back = o.filter(x => x[0] <= bef[0] && bef[0] - x[0] >= 90 && bef[0] - x[0] <= 600).shift();
    if (back) v = Math.min(v, bef[1] + (bef[1] - back[1]) / (bef[0] - back[0]) * (t - bef[0]));
  }
  return {v: Math.round(Math.max(near === bef ? bef[1] : 0, v)), exact: false};
}
function radarItems(d) {
  const W = d.cfg.win;
  return Object.values(d.items).map(i => { const e = fav15(i.obs, W); const last = i.obs[i.obs.length - 1];
    return {...i, f15: e.v, exact: e.exact, cur: last[1], age_min: last[0] / 60, rate: last[1] / Math.max(1, last[0] / 60), hot: e.v != null && e.v >= d.cfg.hot}; });
}
function radarBrands(d) {
  const items = radarItems(d);
  // offre vue (plus récentes) regroupée par marque avec la même clé
  const supplyBy = {}; for (const [b, n] of Object.entries(d.supply || {})) supplyBy[brandKey(b)] = (supplyBy[brandKey(b)] || 0) + n;
  return [...groupBrands(items)].filter(([k]) => k !== "(sans marque)").map(([k, {name: brand, items: its}]) => {
    const f = its.filter(i => i.f15 != null).map(i => i.f15), hot = its.filter(i => i.hot).length, supply = supplyBy[k] || 0;
    const variants = [...new Set(its.map(i => i.brand))];
    const cats = {}; its.forEach(i => cats[i.cat] = (cats[i.cat] || 0) + 1);
    const topCat = Object.entries(cats).sort((a, b) => b[1] - a[1])[0]?.[0] || "";
    return {brand, n: its.length, hot, f15_med: median(f), f15_max: f.length ? Math.max(...f) : 0, f15_mean: f.length ? r1(mean(f), 1) : 0,
      rate: r1(mean(its.map(i => i.rate)), 2), price: median(its.map(i => i.price).filter(Boolean)), supply,
      hit_rate: supply ? r1(100 * hot / Math.max(supply, hot), 1) : null, cat: topCat, variants,
      score: r1(median(f) * Math.min(its.length, 5) / 5 + 2 * hot, 2)};
  }).sort((a, b) => b.score - a.score);
}
async function radarSave() { if (RADAR.data) await IDB.set("radar:current", RADAR.data).catch(() => {}); }
async function radarLoop(token) {
  const d = RADAR.data, cfg = d.cfg;
  const cats = Object.keys(cfg.cats).length ? Object.entries(cfg.cats).map(([k, v]) => [+k, v]) : rootCats();
  const jobs = []; for (const c of cats) for (let p = 1; p <= cfg.pages; p++) jobs.push([...c, p]);
  while (!RADAR.stop) {
    const [cid, cpath, p] = jobs[d.rot % jobs.length]; d.rot++;
    const filters = {catalog_ids: [cid], price_from: cfg.pf, price_to: cfg.pt, status_ids: cfg.st};
    RADAR.log = `${shortCat(cpath)} · page ${p}`;
    const r = await XP.page(filters, "relevance", p); d.requests++;
    const W = cfg.win, maxAge = W + 30;
    for (const it of r.items || []) {
      if (it.promoted) continue;
      const a = it.age_min, rec = d.items[it.item_id];
      if (!rec && a > maxAge) continue;          // trop vieille pour mesurer ses 15 premières minutes
      if (rec && a > 2 * W + 15) continue;
      const x = rec || (d.items[it.item_id] = {item_id: it.item_id, brand: it.brand, title: it.title, price: it.price, url: it.url, photo_url: it.photo_url, cat: cpath, posted_at: it.posted_at, obs: []});
      const last = x.obs[x.obs.length - 1];
      if (!last || a * 60 - last[0] > 20) { x.obs.push([Math.max(0, a * 60), it.favs]); if (x.obs.length > 14) x.obs.splice(1, 1); }
      const e = fav15(x.obs, W);
      if (e.v != null && e.v >= cfg.hot && !x.alerted) {
        x.alerted = true;
        const title = `📡 ${it.brand} : ${it.favs} fav. en ${Math.round(a)} min (≈ ${e.v} à ${W} min)`;
        (RADAR.alerts ||= []).unshift({title, url: it.url, at: Date.now()}); RADAR.alerts = RADAR.alerts.slice(0, 8);
        if (ALERTS.on) { ALERTS.beep(2); ALERTS.notify(title, `${eur(it.price)} · ${shortCat(cpath)}`, it.url, String(it.item_id)); try { navigator.vibrate?.([150, 60, 150]); } catch {} }
      }
    }
    // Offre : 1 fois sur 3, page 1 « plus récentes » pour compter les nouvelles annonces par marque.
    if (d.ticks % 3 === 0) {
      await sleep(XP.demo ? 30 : 700 + Math.random() * 600);
      const n = await XP.page(filters, "newest_first", 1); d.requests++;
      for (const it of n.items || []) if (!it.promoted && !d.supplySeen[it.item_id]) { d.supplySeen[it.item_id] = 1; d.supply[it.brand] = (d.supply[it.brand] || 0) + 1; }
      const ids = Object.keys(d.supplySeen); if (ids.length > 60000) ids.slice(0, 20000).forEach(k => delete d.supplySeen[k]);
    }
    d.ticks++;
    // purge : on garde les annonces mesurées (et toutes celles « chaudes »), on oublie le reste après 2 h
    const now = Date.now() / 1000;
    if (d.ticks % 20 === 0) for (const [k, i] of Object.entries(d.items)) if (now - i.posted_at > 7200 && !i.alerted && i.obs.length < 2) delete d.items[k];
    if (d.ticks % 5 === 0) await radarSave();
    if (location.hash.startsWith("#radar")) drawRadar(renderToken);
    if (cfg.hours && now - d.started > cfg.hours * 3600) break;
    await sleep(XP.demo ? 300 : cfg.pace * 1000 * (0.8 + Math.random() * 0.4));
  }
  RADAR.running = false; await radarSave(); keepAwake(false);
  if (location.hash.startsWith("#radar")) viewRadar(renderToken);
}
function drawRadar(token) {
  const d = RADAR.data; if (!d || !$("#rres")) return;
  const items = radarItems(d).filter(i => i.f15 != null).sort((a, b) => b.f15 - a.f15);
  const brands = radarBrands(d), hours = Math.max(1 / 60, (Date.now() / 1000 - d.started) / 3600);
  $("#rres").innerHTML = `
  <div class="card">
    <div class="phase"><b>${RADAR.running ? "📡 Radar actif" : "Radar arrêté"} — ${esc(RADAR.log || "")}</b><span class="muted small">depuis ${dur(hours * 3600)} · ${nf(d.requests)} requêtes</span></div>
    <div class="stats">
      <div class="stat"><div class="l">Annonces jeunes captées</div><div class="v">${nf(Object.keys(d.items).length)}</div></div>
      <div class="stat"><div class="l">Mesurées à ${d.cfg.win} min</div><div class="v">${nf(items.length)}</div></div>
      <div class="stat"><div class="l">🔥 ≥ ${d.cfg.hot} fav. à ${d.cfg.win} min</div><div class="v" style="color:var(--err)">${nf(items.filter(i => i.hot).length)}</div></div>
      <div class="stat"><div class="l">Marques</div><div class="v">${nf(brands.length)}</div></div>
    </div>
    ${(RADAR.alerts || []).length ? `<div class="items" style="margin-top:12px">${RADAR.alerts.map(a => `<a class="it hot" href="${esc(a.url)}" target="_blank" rel="noopener"><span class="mid"><span class="t">${esc(a.title)}</span><span class="s">${new Date(a.at).toLocaleTimeString("fr-FR", {hour: "2-digit", minute: "2-digit"})}</span></span></a>`).join("")}</div>` : ""}
  </div>
  <div class="card"><div class="row"><h2 style="margin:0">Classement des marques</h2><span class="spacer"></span><button class="btn ghost sm" id="rcsvb">⇩ CSV marques</button></div>
    <div class="hint" style="margin:6px 0 10px">Favoris à ${d.cfg.win} min des annonces de la marque captées par le radar. « Nouv. annonces » = annonces de la marque vues dans les « plus récentes » (offre) : un fort ratio 🔥 / offre = forte demande.</div><div id="rtb"></div></div>
  <div class="card"><div class="row"><h2 style="margin:0">Annonces : favoris dans les ${d.cfg.win} premières minutes</h2><span class="spacer"></span><button class="btn ghost sm" id="rcsvi">⇩ CSV annonces</button></div><div id="rti" style="margin-top:10px"></div></div>`;
  const t = () => {
    $("#rtb").innerHTML = table("radar-brands", [
      {key: "rank", label: "#", num: true, nosort: true, fmt: r => r._rank},
      {key: "brand", label: "Marque", fmt: r => `<b>${esc(r.brand)}</b>${r.variants?.length > 1 ? ` <span class="small muted" title="${esc(r.variants.join(", "))}">+${r.variants.length - 1} variante(s)</span>` : ""}`},
      {key: "score", label: "Score", num: true, fmt: r => `<span class="score">${nf(r.score, 1)}</span>`},
      {key: "hot", label: "🔥", num: true}, {key: "n", label: "Captées", num: true},
      {key: "f15_med", label: `Fav. ${d.cfg.win} min méd.`, num: true, fmt: r => nf(r.f15_med, 1)},
      {key: "f15_max", label: "Max", num: true}, {key: "rate", label: "Fav./min", num: true, fmt: r => nf(r.rate, 2)},
      {key: "supply", label: "Nouv. annonces", num: true}, {key: "hit_rate", label: "🔥 / offre", num: true, fmt: r => r.hit_rate == null ? "–" : nf(r.hit_rate, 1) + " %"},
      {key: "price", label: "Prix méd.", num: true, fmt: r => eur(r.price)}, {key: "cat", label: "Catégorie", fmt: r => esc(shortCat(r.cat))},
    ], brands.slice(0, 200).map((b, i) => ({...b, _rank: i + 1})), "score", r => r.hot ? "hit" : "");
    $("#rti").innerHTML = itemsTable("radar-items", items.slice(0, 300), [
      {key: "f15", label: `Fav. ${d.cfg.win} min`, num: true, fmt: i => `<b>${i.f15}</b>${i.exact ? "" : ` <span class="tag p" title="Projection : pas de relevé assez proche de ${d.cfg.win} min">≈</span>`}${i.hot ? ' <span class="tag hot">🔥</span>' : ""}`},
      {key: "cur", label: "Fav. actuels", num: true}, {key: "age_min", label: "Âge", num: true, fmt: i => fmtAge(i.age_min)},
      {key: "rate", label: "Fav./min", num: true, fmt: i => nf(i.rate, 2)}]);
    bindTables($("#rres"), t);
  };
  t();
  $("#rcsvb").onclick = () => downloadCsv([["rang", "marque", "score", "annonces 🔥", "annonces captées", `favoris ${d.cfg.win} min médians`, "max", "favoris/min", "nouvelles annonces vues", "🔥/offre (%)", "prix médian", "catégorie principale"],
    ...brands.map((b, i) => [i + 1, b.brand, csvNum(b.score), b.hot, b.n, csvNum(b.f15_med, 1), b.f15_max, csvNum(b.rate), b.supply, csvNum(b.hit_rate, 1), csvNum(b.price), b.cat])], "radar_marques.csv");
  $("#rcsvi").onclick = () => downloadCsv([["id", "marque", "titre", "prix", `favoris ${d.cfg.win} min`, "mesure", "favoris actuels", "âge (min)", "catégorie", "lien"],
    ...items.map(i => [i.item_id, i.brand, i.title, csvNum(i.price), i.f15, i.exact ? "mesurée" : "projetée", i.cur, Math.round(i.age_min), i.cat, i.url])], "radar_annonces.csv");
}
async function viewRadar(token) {
  await loadCats();
  if (!RADAR.data) RADAR.data = await IDB.get("radar:current").catch(() => null);
  const f = RADAR.data?.cfg || (() => { try { return JSON.parse(localStorage.getItem("vnw-radar")) || {}; } catch { return {}; } })();
  $("#app").innerHTML = `
  <form class="card" id="rf">
    <h2>📡 Radar — ce qui cartonne en ce moment</h2>
    <p class="muted small" style="margin-top:-8px">Tourne en continu sur tout Vinted (ou tes catégories) : repère les annonces qui prennent le plus de favoris dans leurs ${f.win ?? 15} premières minutes et classe les marques sur la durée. Laisse la page ouverte.</p>
    <label>Catégories surveillées</label><div id="rcat"></div>
    <div class="fields" style="margin-top:12px">
      <div><label>Prix min (€)</label><input type="number" id="rpf" value="${f.pf ?? ""}"></div>
      <div><label>Prix max (€)</label><input type="number" id="rpt" value="${f.pt ?? ""}"></div>
      <div><label>🔥 si favoris à 15 min ≥</label><input type="number" id="rhot" min="1" value="${f.hot ?? 20}"></div>
    </div>
    <details style="margin-top:12px"><summary class="small" style="cursor:pointer;font-weight:600">Réglages avancés</summary>
      <div class="fields" style="margin-top:10px">
        <div><label>Fenêtre (min)</label><input type="number" id="rwin" min="5" max="60" value="${f.win ?? 15}"></div>
        <div><label>Pages par catégorie</label><input type="number" id="rpg" min="1" max="5" value="${f.pages ?? 3}"></div>
        <div><label>Secondes entre requêtes</label><input type="number" id="rpace" min="4" value="${f.pace ?? 8}"></div>
        <div><label>Durée max (h, vide = illimitée)</label><input type="number" id="rh" min="0" step="0.5" value="${f.hours ?? ""}"></div>
      </div>
      <label style="margin-top:10px">État</label><div id="rst"></div>
      <label class="check" style="margin-top:10px"><input type="checkbox" id="rdemo" ${f.demo ? "checked" : ""}><span>Démo (données simulées)</span></label>
      <div class="hint" id="rcost"></div>
    </details>
    <div class="row" style="margin-top:14px">
      <button type="button" class="btn ${ALERTS.on ? "" : "ghost"} sm" id="ralerts">${ALERTS.on ? "🔔 Alertes activées" : "🔕 Activer les alertes"}</button>
      <span class="spacer"></span>
      ${RADAR.data && !RADAR.running ? `<button type="button" class="btn ghost" id="rreset">Nouveau radar</button>` : ""}
      <button class="btn" id="rgo">${RADAR.running ? "■ Arrêter" : RADAR.data ? "▶ Reprendre" : "📡 Démarrer le radar"}</button>
    </div>
  </form>
  <div id="rres">${RADAR.data ? "" : `<div class="card empty">Le radar n'a pas encore tourné.</div>`}</div>`;
  const picker = catPicker($("#rcat"), new Map(Object.entries(f.cats || {}).map(([k, v]) => [+k, v])), {placeholder: "Vide = les 9 grandes catégories de Vinted"});
  const st = statusPicker($("#rst"), f.st || []);
  const cost = () => { const n = picker.get().size || rootCats().length, pg = +$("#rpg").value || 3, pace = +$("#rpace").value || 8;
    $("#rcost").textContent = `${n} catégorie(s) × ${pg} page(s) : chaque recherche revient toutes les ~${dur(n * pg * pace * 1.33)} (≈ ${nf(60 / pace * 1.33, 1)} requêtes/min). Sur les grandes catégories, une annonce quitte les premières pages vers 5-10 min : sa valeur à ${+$("#rwin").value || 15} min est alors projetée (≈, prudente). Pour une mesure exacte d'une niche précise, utilise l'onglet Veille. Astuce : un prix min (ex. 10 €) écarte les petits articles à 2 € très « likés ».`; };
  $("#rf").addEventListener("input", cost); setTimeout(cost, 300);
  $("#ralerts").onclick = async () => { await ALERTS.toggle({session: {id: "radar"}, alerts: []}); viewRadar(token); };
  if ($("#rreset")) $("#rreset").onclick = async () => { if (!confirm("Effacer le radar en cours et en démarrer un nouveau ?")) return; RADAR.data = null; await IDB.del("radar:current"); viewRadar(token); };
  if (RADAR.data) drawRadar(token);
  XP.onStatus = m => { RADAR.log = m; drawRadar(token); };
  $("#rf").onsubmit = async e => {
    e.preventDefault();
    if (RADAR.running) { RADAR.stop = true; $("#rgo").textContent = "Arrêt…"; return; }
    const num = id => $(id).value === "" ? null : Number($(id).value);
    const cfg = {cats: Object.fromEntries(picker.get()), win: num("#rwin") ?? 15, hot: num("#rhot") ?? 20, pages: Math.min(5, num("#rpg") ?? 3),
      pace: Math.max(4, num("#rpace") ?? 8), pf: num("#rpf"), pt: num("#rpt"), hours: num("#rh"), st: st.get(), demo: $("#rdemo").checked};
    try { localStorage.setItem("vnw-radar", JSON.stringify(cfg)); } catch {}
    if (!RADAR.data || JSON.stringify(RADAR.data.cfg.cats) !== JSON.stringify(cfg.cats)) RADAR.data = radarNew(cfg); else RADAR.data.cfg = cfg;
    XP.demo = cfg.demo; RADAR.running = true; RADAR.stop = false; keepAwake(true);
    $("#rgo").textContent = "■ Arrêter"; drawRadar(token);
    radarLoop(token);
  };
}
