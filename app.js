// Morning Brief — renders data.json into cards and handles the scenario builder.

const fmtDate = (iso) => {
  if (!iso) return "";
  try {
    const d = new Date(iso);
    return d.toLocaleString(undefined, {
      year: "numeric", month: "short", day: "numeric",
      hour: "2-digit", minute: "2-digit",
    });
  } catch { return iso; }
};

function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k === "html") node.innerHTML = v;
    else node.setAttribute(k, v);
  }
  for (const c of children) if (c) node.appendChild(c);
  return node;
}

const MOVE_LABEL = {
  small: "~1-2%",
  moderate: "~3-5%",
  large: ">5%",
};

const LIKELIHOOD_LABEL = {
  most_likely: "Most likely",
  plausible: "Plausible",
  less_likely: "Less likely",
  tail_risk: "Tail risk",
};

function renderAffectedRow(item) {
  const li = document.createElement("li");
  li.appendChild(el("span", { class: "tk", text: item.ticker }));
  li.appendChild(el("span", { class: `dir ${item.direction}`, text: item.direction }));
  if (item.expected_move) {
    const moveLbl = MOVE_LABEL[item.expected_move] || item.expected_move;
    li.appendChild(el("span", { class: `move ${item.expected_move}`, text: moveLbl }));
  } else {
    li.appendChild(el("span", { class: "move none", text: "" }));
  }
  li.appendChild(el("span", { class: "reason", text: item.reason }));
  return li;
}

function fillScenario(card, key, scenario) {
  if (!scenario) return;
  const block = card.querySelector(`.scenario.${key}`);
  block.querySelector(".scenario-label").textContent = scenario.label ? `· ${scenario.label}` : "";
  block.querySelector(".condition").textContent = scenario.condition || "";
  block.querySelector(".horizon").textContent = scenario.horizon ? `· ${scenario.horizon}` : "";
  const badge = block.querySelector(".likelihood-badge");
  if (badge) {
    if (scenario.likelihood || scenario.likelihood_pct != null) {
      const lbl = LIKELIHOOD_LABEL[scenario.likelihood] || scenario.likelihood || "";
      const pct = scenario.likelihood_pct != null ? ` ${scenario.likelihood_pct}%` : "";
      badge.textContent = `${lbl}${pct}`;
      badge.className = `likelihood-badge ${scenario.likelihood || ""}`;
    } else {
      badge.textContent = "";
    }
  }
  const list = block.querySelector(".scenario-tickers");
  list.innerHTML = "";
  (scenario.tickers || []).forEach((t) => list.appendChild(renderAffectedRow(t)));
}

function renderCard(payload) {
  const tpl = document.getElementById("card-template");
  const card = tpl.content.firstElementChild.cloneNode(true);
  const c = payload.card;

  card.dataset.impact = c.impact || "medium";
  card.dataset.theme = c.theme || "Other";

  card.querySelector(".theme-tag").textContent = c.theme || "Other";
  const impactTag = card.querySelector(".impact-tag");
  impactTag.textContent = (c.impact || "medium").toUpperCase();
  impactTag.classList.add(c.impact || "medium");

  card.querySelector(".card-headline").textContent = c.plain_headline || "(no headline)";
  card.querySelector(".what-happened.concise").textContent = c.what_happened || "";
  card.querySelector(".what-happened.verbose").textContent = c.what_happened_verbose || c.what_happened || "";

  const affList = card.querySelector(".affected");
  (c.affected || []).forEach((a) => affList.appendChild(renderAffectedRow(a)));

  const sc = c.scenarios || {};
  fillScenario(card, "base", sc.base);
  fillScenario(card, "upside", sc.upside);
  fillScenario(card, "downside", sc.downside);

  const item = payload.source_item || {};
  const link = card.querySelector(".source-link");
  link.href = item.link || "#";
  link.textContent = `${item.source || "source"} — read original`;

  // expand on click
  const head = card.querySelector(".card-head");
  head.addEventListener("click", () => {
    card.classList.toggle("expanded");
    card.querySelector(".expand-btn").textContent = card.classList.contains("expanded") ? "–" : "+";
  });

  return card;
}

function applyVerbose(verbose) {
  document.querySelectorAll(".what-happened.concise").forEach((p) => p.hidden = verbose);
  document.querySelectorAll(".what-happened.verbose").forEach((p) => p.hidden = !verbose);
}

// ----- scenario builder -----

function renderBuiltScenario(data) {
  const root = document.getElementById("scenario-result");
  root.innerHTML = "";
  if (!data || data.ok === false) {
    root.appendChild(el("div", { class: "err", text: (data && data.error) || "Couldn't generate a scenario for this." }));
    return;
  }
  const box = el("div", { class: "built-card" });
  box.appendChild(el("h3", { text: data.plain_headline || "Scenario" }));
  if (data.interpretation) box.appendChild(el("p", { class: "interp", text: data.interpretation }));

  const sc = data.scenarios || {};
  ["base", "upside", "downside"].forEach((key) => {
    const s = sc[key];
    if (!s) return;
    const block = el("div", { class: `scenario ${key}` });
    const title = key === "base" ? "Base case" : key === "upside" ? "Upside" : "Downside";
    const likelihoodLbl = LIKELIHOOD_LABEL[s.likelihood] || s.likelihood || "";
    const pct = s.likelihood_pct != null ? ` ${s.likelihood_pct}%` : "";
    const badgeHtml = (likelihoodLbl || pct)
      ? ` <span class="likelihood-badge ${s.likelihood || ""}">${likelihoodLbl}${pct}</span>`
      : "";
    block.appendChild(el("div", { class: "scenario-title", html: `<span class="scenario-kind">${title}</span>${badgeHtml} <span class="scenario-label">${s.label ? "· " + s.label : ""}</span>` }));
    const meta = el("div", { class: "scenario-meta" });
    meta.appendChild(el("span", { class: "condition", text: s.condition || "" }));
    if (s.horizon) meta.appendChild(el("span", { class: "horizon", text: `· ${s.horizon}` }));
    block.appendChild(meta);
    const list = el("ul", { class: "scenario-tickers" });
    (s.tickers || []).forEach((t) => list.appendChild(renderAffectedRow(t)));
    block.appendChild(list);
    box.appendChild(block);
  });
  root.appendChild(box);
}

async function runScenario() {
  const btn = document.getElementById("run-scenario");
  const input = document.getElementById("hypothesis");
  const hypothesis = input.value.trim();
  const result = document.getElementById("scenario-result");

  if (!hypothesis) {
    result.innerHTML = "";
    result.appendChild(el("div", { class: "err", text: "Type a hypothetical first." }));
    return;
  }

  btn.disabled = true;
  result.innerHTML = "";
  result.appendChild(el("div", { class: "muted", text: "Generating scenario…" }));

  try {
    const resp = await fetch("/api/scenario", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ hypothesis }),
    });
    const data = await resp.json();
    renderBuiltScenario(data);
  } catch (err) {
    result.innerHTML = "";
    result.appendChild(el("div", { class: "err", text: `Request failed: ${err.message}. Is scenario.py running?` }));
  } finally {
    btn.disabled = false;
  }
}

// ----- boot -----

function buildWatchlist(cards) {
  // Aggregate tickers across today's cards. Score by:
  //   high impact card = 3, medium = 2, low = 1
  //   plus +1 per appearance in a scenario
  //   plus magnitude weight: large = 2, moderate = 1, small = 0
  const IMPACT_W = { high: 3, medium: 2, low: 1 };
  const MOVE_W = { large: 2, moderate: 1, small: 0 };

  const agg = new Map(); // ticker -> { score, dirs:{benefits,hurts,mixed}, reasons:Set, biggestMove, impact }
  for (const c of cards) {
    const card = c.card || {};
    const impact = card.impact || "medium";
    const impactW = IMPACT_W[impact] || 1;
    const sources = [...(card.affected || [])];
    for (const sc of Object.values(card.scenarios || {})) {
      if (sc && sc.tickers) sources.push(...sc.tickers);
    }
    for (const t of sources) {
      if (!t.ticker) continue;
      const entry = agg.get(t.ticker) || { score: 0, dirs: {}, reasons: new Set(), biggestMove: "small", impact: impact, headlines: new Set() };
      entry.score += impactW + (MOVE_W[t.expected_move] || 0);
      entry.dirs[t.direction] = (entry.dirs[t.direction] || 0) + 1;
      if (t.reason) entry.reasons.add(t.reason);
      if (t.expected_move) {
        if ((MOVE_W[t.expected_move] || 0) > (MOVE_W[entry.biggestMove] || 0)) entry.biggestMove = t.expected_move;
      }
      if (card.plain_headline) entry.headlines.add(card.plain_headline);
      agg.set(t.ticker, entry);
    }
  }
  const ranked = [...agg.entries()]
    .map(([ticker, v]) => {
      // dominant direction
      const dir = Object.entries(v.dirs).sort((a, b) => b[1] - a[1])[0]?.[0] || "mixed";
      const reason = [...v.reasons][0] || "";
      return { ticker, score: v.score, dir, move: v.biggestMove, reason, headline: [...v.headlines][0] || "" };
    })
    .sort((a, b) => b.score - a.score)
    .slice(0, 8);
  return ranked;
}

function renderWatchlist(items) {
  const root = document.getElementById("watchlist");
  const section = document.getElementById("watchlist-section");
  root.innerHTML = "";
  if (!items.length) {
    section.hidden = true;
    return;
  }
  for (const it of items) {
    const row = el("div", { class: "watch-row" });
    row.appendChild(el("span", { class: "tk", text: it.ticker }));
    row.appendChild(el("span", { class: `dir ${it.dir}`, text: it.dir }));
    const moveLbl = MOVE_LABEL[it.move] || it.move || "";
    row.appendChild(el("span", { class: `move ${it.move || "small"}`, text: moveLbl }));
    row.appendChild(el("span", { class: "watch-reason", text: it.reason }));
    root.appendChild(row);
  }
  section.hidden = false;
}

async function loadBrief() {
  const cardsRoot = document.getElementById("cards");
  const empty = document.getElementById("empty-state");
  const hero = document.getElementById("hero-summary");
  const generatedAt = document.getElementById("generated-at");

  try {
    const resp = await fetch("data.json", { cache: "no-store" });
    if (!resp.ok) throw new Error("no data.json");
    const data = await resp.json();

    hero.textContent = data.hero_summary || "";
    generatedAt.textContent = data.generated_at ? `Updated ${fmtDate(data.generated_at)}` : "";

    const watchlist = buildWatchlist(data.cards || []);
    renderWatchlist(watchlist);

    (data.cards || []).forEach((c) => cardsRoot.appendChild(renderCard(c)));

    if (!data.cards || data.cards.length === 0) {
      empty.hidden = false;
    }
  } catch (err) {
    hero.textContent = "No brief loaded yet. Run the pipeline to populate it.";
    empty.hidden = false;
  }
}

document.addEventListener("DOMContentLoaded", () => {
  loadBrief();

  document.getElementById("explain-toggle").addEventListener("change", (e) => {
    applyVerbose(e.target.checked);
  });

  document.getElementById("run-scenario").addEventListener("click", runScenario);

  document.querySelectorAll(".chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      document.getElementById("hypothesis").value = chip.dataset.example;
      document.getElementById("hypothesis").focus();
    });
  });
});
