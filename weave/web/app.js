/* Weave workspace — FLOAT system.
 *
 * The source system uses GSAP + ScrollTrigger for entrances. This page ships
 * with no build step and no external requests, so the same choreography is
 * driven by IntersectionObserver and CSS transitions: identical end states,
 * identical reduced-motion branches. The hero particle field is raw canvas,
 * exactly as in the source.
 *
 * The rule is unchanged: JS for entrances and ambient life, CSS for interaction.
 */

(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const esc = (value) =>
    String(value ?? "").replace(/[&<>"']/g, (ch) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch])
    );

  // Mark the document as scripted before first paint so reveal states apply.
  // Without JS the page renders fully visible rather than blank.
  document.documentElement.classList.add("js");

  const REDUCED = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const state = { lastQuery: null, busy: false };

  const num = (value) => Number(value ?? 0).toLocaleString("en-US");
  const pct = (value) => `${Math.round(Number(value ?? 0) * 100)}%`;

  // ---------------------------------------------------------------- network

  async function api(path, options = {}) {
    const response = await fetch(path, {
      headers: { "content-type": "application/json" },
      ...options,
    });
    if (!response.ok) {
      let detail = `${response.status} ${response.statusText}`;
      try {
        const body = await response.json();
        if (body.detail) detail = body.detail;
      } catch (_) {
        /* keep the status line */
      }
      throw new Error(detail);
    }
    return response.json();
  }

  let toastTimer = null;
  function toast(message) {
    const node = $("toast");
    node.textContent = message;
    node.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => (node.hidden = true), 2800);
  }

  // ----------------------------------------------------------------- motion

  /* Scroll reveals: rise on entry, with a small per-item stagger. */
  function initReveals() {
    const items = document.querySelectorAll("[data-reveal]");
    if (REDUCED) {
      items.forEach((el) => el.classList.add("is-in"));
      return;
    }
    const observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry, index) => {
          if (!entry.isIntersecting) return;
          const delay = Math.min(index, 6) * 70;
          setTimeout(() => entry.target.classList.add("is-in"), delay);
          observer.unobserve(entry.target);
        });
      },
      { rootMargin: "0px 0px -12% 0px", threshold: 0.1 }
    );
    items.forEach((el) => observer.observe(el));
  }

  /* Hero stagger-in on load, ahead of the scroll observer. */
  function heroStagger() {
    const items = document.querySelectorAll(".hero [data-reveal]");
    items.forEach((el, index) => {
      setTimeout(() => el.classList.add("is-in"), REDUCED ? 0 : 100 + index * 100);
    });
  }

  /* The thesis diagram draws itself once, on scroll-in. */
  function initThesis() {
    const section = $("thesis");
    if (!section) return;
    if (REDUCED) {
      section.classList.add("is-drawn");
      return;
    }
    const observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (!entry.isIntersecting) return;
          section.classList.add("is-drawn");
          observer.disconnect();
        });
      },
      { threshold: 0.35 }
    );
    observer.observe(section);
  }

  /* Hero particle field: drifting nodes, lines to the nearest few neighbours.
     Dots are coloured by memory layer, so the ambient decoration reads as the
     product's own subject rather than generic particles. */
  function initParticles() {
    const canvas = $("particles");
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    const LAYER = ["#c9bfea", "#f2a683", "#b8e6a8"];
    let width = 0;
    let height = 0;
    let dots = [];
    let raf = null;

    function resize() {
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      const rect = canvas.getBoundingClientRect();
      width = rect.width;
      height = rect.height;
      canvas.width = Math.round(width * dpr);
      canvas.height = Math.round(height * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

      const count = Math.min(150, Math.round((width * height) / 9000));
      dots = Array.from({ length: count }, () => ({
        x: Math.random() * width,
        y: Math.random() * height,
        vx: (Math.random() - 0.5) * 0.22,
        vy: (Math.random() - 0.5) * 0.22,
        r: 1.4 + Math.random() * 1.7,
        c: LAYER[Math.floor(Math.random() * LAYER.length)],
      }));
    }

    function draw() {
      ctx.clearRect(0, 0, width, height);

      for (let i = 0; i < dots.length; i += 1) {
        const a = dots[i];
        // Link to at most the three nearest neighbours within 150px.
        const near = [];
        for (let j = i + 1; j < dots.length; j += 1) {
          const b = dots[j];
          const d = Math.hypot(a.x - b.x, a.y - b.y);
          if (d < 150) near.push({ b, d });
        }
        near.sort((p, q) => p.d - q.d);
        near.slice(0, 3).forEach(({ b, d }) => {
          ctx.strokeStyle = `rgba(124, 108, 245, ${0.16 * (1 - d / 150)})`;
          ctx.lineWidth = 1;
          ctx.beginPath();
          ctx.moveTo(a.x, a.y);
          ctx.lineTo(b.x, b.y);
          ctx.stroke();
        });
      }

      dots.forEach((dot) => {
        ctx.fillStyle = dot.c;
        ctx.globalAlpha = 0.75;
        ctx.beginPath();
        ctx.arc(dot.x, dot.y, dot.r, 0, Math.PI * 2);
        ctx.fill();
        ctx.globalAlpha = 1;
      });
    }

    function tick() {
      dots.forEach((dot) => {
        dot.x += dot.vx;
        dot.y += dot.vy;
        if (dot.x < 0 || dot.x > width) dot.vx *= -1;
        if (dot.y < 0 || dot.y > height) dot.vy *= -1;
      });
      draw();
      raf = requestAnimationFrame(tick);
    }

    resize();
    window.addEventListener("load", () => {
      if (raf) cancelAnimationFrame(raf);
      resize();
      if (REDUCED) draw();
      else tick();
    });
    if (REDUCED) {
      draw(); // a single static frame is the defined end state
    } else {
      tick();
    }

    let resizeTimer = null;
    window.addEventListener("resize", () => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => {
        if (raf) cancelAnimationFrame(raf);
        resize();
        if (REDUCED) draw();
        else tick();
      }, 200);
    });
  }

  // -------------------------------------------------------------- dashboard

  async function refreshHealth() {
    try {
      const health = await api("/health");
      const config = health.config || {};
      const backend = String(config.backend || "unknown").toUpperCase();

      // Honesty rule: the embedded engine is never dressed up as HydraDB.
      const backendBadge = $("badge-backend");
      backendBadge.textContent = `BACKEND ${backend}`;
      backendBadge.className = `tag ${backend === "HYDRA" ? "tag-signal" : "tag-quiet"}`;

      const extractionBadge = $("badge-extraction");
      extractionBadge.textContent = health.llm_configured
        ? "EXTRACTION LLM"
        : "EXTRACTION RULE-BASED";
      extractionBadge.className = `tag ${health.llm_configured ? "tag-signal" : "tag-quiet"}`;

      $("footer-config").textContent =
        `${backend} · ${config.llm} · abstention ≥ ${config.abstention_threshold} · ` +
        `context ≤ ${num(config.max_context_tokens)}`;
    } catch (_) {
      $("badge-backend").textContent = "BACKEND OFFLINE";
    }
  }

  async function refreshStats() {
    const stats = await api("/stats");
    const sessions = stats.by_label?.Session || 0;

    $("m-nodes").textContent = num(stats.nodes);
    $("m-edges").textContent = num(stats.edges);
    $("m-sessions").textContent = num(sessions);
    $("m-current").textContent = num(stats.current_facts);
    $("m-superseded").textContent = num(stats.superseded_facts);
    $("m-conflicts").textContent = num(stats.open_conflicts);

    const badge = $("badge-source");
    if (sessions > 0) {
      badge.className = "tag tag-signal";
      badge.textContent = `${sessions} SESSIONS`;
      const layers = stats.by_layer || {};
      $("hero-note").textContent =
        `${num(layers.episodic)} episodic · ${num(layers.semantic)} semantic · ` +
        `${num(layers.procedural)} procedural nodes, all traversable.`;
    } else {
      badge.className = "tag tag-quiet";
      badge.textContent = "NO DATA";
      $("hero-note").textContent = "Load the demo memory to populate all three layers.";
    }
  }

  // ------------------------------------------------------------------ query

  const SUGGESTIONS = [
    "What language do I prefer for pipelines?",
    "Where do I live?",
    "Where did I live before?",
    "What database do I use?",
    "When did I switch to Go?",
    "Do I like coffee?",
    "What is my blood type?",
  ];

  function renderSuggestions() {
    $("suggestions").innerHTML = SUGGESTIONS.map(
      (text) => `<button type="button" class="chip">${esc(text)}</button>`
    ).join("");
    $("suggestions")
      .querySelectorAll(".chip")
      .forEach((chip) =>
        chip.addEventListener("click", () => {
          $("q").value = chip.textContent;
          runQuery();
        })
      );
  }

  function renderVerdict(result) {
    const card = $("verdict");
    const label = $("verdict-label");

    if (result.abstained) {
      card.className = "verdict verdict-abstained";
      label.textContent = "NO ANSWER RETURNED";
      $("verdict-state").textContent = "REFUSED BEFORE GENERATION";
    } else if (result.abstention?.signals?.open_conflicts > 0) {
      card.className = "verdict verdict-conflict";
      label.textContent = "ANSWERED · UNRESOLVED CONFLICT";
      $("verdict-state").textContent = "REVIEW";
    } else {
      card.className = "verdict verdict-answered";
      label.textContent = "ANSWERED · GROUNDED";
      $("verdict-state").textContent = "ALL CHECKS PASSED";
    }

    $("verdict-answer").textContent = result.answer;
    $("verdict-confidence").textContent = pct(result.confidence);
    $("verdict-generator").textContent = `VIA ${String(result.generator || "").toUpperCase()}`;

    const signals = result.abstention?.signals || {};
    const rows = [
      ["Query type", result.query_type],
      ["Path", result.retrieval_path],
      ["Context tokens", num(result.tokens_used)],
      ["Topical overlap", pct(signals.topical_overlap)],
      ["Results", num(signals.result_count)],
      ["Score / threshold",
        `${Number(result.abstention?.score ?? 0).toFixed(2)} / ${Number(result.abstention?.threshold ?? 0).toFixed(2)}`],
    ];

    let detail = `<div class="signal-grid">${rows
      .map(
        ([key, value]) =>
          `<div><span class="label">${esc(key)}</span>
           <span class="signal-value">${esc(value)}</span></div>`
      )
      .join("")}</div>`;

    if (result.abstained && result.abstention_reasons?.length) {
      detail += `<div class="stack" style="margin-top:14px;gap:10px">${result.abstention_reasons
        .map((reason) => `<div class="reason">${esc(reason)}</div>`)
        .join("")}</div>`;
    }

    detail += `<p class="label" style="margin:14px 0 0">ROUTING ${esc(
      result.path_reason || "default"
    )} &middot; LAYERS ${esc((result.layers_touched || []).join(" + ") || "none")}</p>`;

    $("verdict-detail").innerHTML = detail;
  }

  function renderEvidence(result) {
    const container = $("evidence");
    const items = result.evidence || [];
    // Show the pruned count against the full traversal, so a short list never
    // reads as a shallow walk.
    const walked = result.retrieved_count ?? items.length;
    $("evidence-state").textContent =
      walked > items.length ? `${items.length} OF ${walked} NODES` : `${items.length} NODES`;

    if (!items.length) {
      container.innerHTML = `<div class="empty">The traversal returned nothing for this question.</div>`;
      return;
    }

    container.innerHTML = items
      .map((item) => {
        const isFact = item.kind === "fact";
        const superseded = isFact && !item.is_current;
        const tag = isFact
          ? item.is_current
            ? '<span class="tag tag-coral">Current fact</span>'
            : '<span class="tag tag-quiet">Superseded</span>'
          : '<span class="tag tag-lav">Excerpt</span>';

        // How the retriever reached this: wording, meaning (embedding), or a
        // graph edge alone. The last is the interesting one -- it is a fact
        // with nothing in common with the question, kept because the graph
        // says it occupies the same slot.
        // Pastels are reserved for layer identity, so these use the neutral
        // pair -- signal for the graph link, which is the notable case.
        const MATCH = {
          both: ["tag-quiet", "wording + meaning"],
          meaning: ["tag-quiet", "semantic match"],
          graph: ["tag-signal", "graph link"],
        };
        const match = MATCH[item.matched_by];

        const meta = [];
        if (item.session_label || item.session_id)
          meta.push(`<span class="data">${esc(item.session_label || item.session_id)}</span>`);
        if (isFact) {
          meta.push(`<span class="label">conf ${pct(item.confidence)}</span>`);
          if (item.superseded_by?.length)
            meta.push(`<span class="label">replaced by ${esc(item.superseded_by.join(", "))}</span>`);
          if (item.supersedes?.length)
            meta.push(`<span class="label">replaced ${esc(item.supersedes.join(", "))}</span>`);
        } else if (item.speaker) {
          meta.push(`<span class="label">${esc(item.speaker)}</span>`);
        }

        return `<article class="evidence ${superseded ? "evidence-superseded" : ""}">
          <div class="evidence-top">${tag}
            ${match ? `<span class="tag ${match[0]}" title="lexical ${Number(item.lexical ?? 0).toFixed(2)} · semantic ${Number(item.semantic ?? 0).toFixed(2)}">${match[1]}</span>` : ""}
            <span class="evidence-score">${Number(item.score ?? 0).toFixed(2)}</span></div>
          <p class="evidence-text" style="margin:0">${esc(item.text)}</p>
          <div class="evidence-meta">${meta.join("")}</div>
        </article>`;
      })
      .join("");
  }

  async function runQuery() {
    const text = $("q").value.trim();
    if (!text || state.busy) return;

    state.busy = true;
    const button = $("btn-ask");
    button.disabled = true;
    button.innerHTML = '<span class="spinner"></span> Running';
    $("console-state").textContent = "TRAVERSING";

    try {
      const result = await api("/query", {
        method: "POST",
        body: JSON.stringify({
          query: text,
          retrieval_path: $("path").value || null,
          max_tokens: Number($("budget").value),
        }),
      });
      state.lastQuery = result;
      renderVerdict(result);
      renderEvidence(result);
      $("feedback-block").hidden = false;
      $("feedback-note").textContent = "Trains the router";
      $("console-state").textContent = `${result.latency_ms} MS`;
    } catch (error) {
      toast(`Query failed · ${error.message}`);
      $("console-state").textContent = "ERROR";
    } finally {
      state.busy = false;
      button.disabled = false;
      button.textContent = "Run query";
    }
  }

  async function sendFeedback(success) {
    if (!state.lastQuery) return;
    const result = state.lastQuery;
    try {
      await api("/feedback", {
        method: "POST",
        body: JSON.stringify({
          query_id: result.query_id,
          query_type: result.query_type,
          retrieval_path: result.retrieval_path,
          success,
          latency_ms: result.latency_ms,
          tokens_used: result.tokens_used,
          abstained: result.abstained,
        }),
      });
      $("feedback-note").textContent = success ? "RECORDED · SUCCESS" : "RECORDED · FAILURE";
      await refreshRouting();
      toast("Outcome recorded");
    } catch (error) {
      toast(`Feedback failed · ${error.message}`);
    }
  }

  // --------------------------------------------------------------- timeline

  async function refreshTimeline() {
    const data = await api("/facts?limit=60");
    const facts = data.facts || [];
    $("timeline-state").textContent = `${facts.length} FACTS`;

    if (!facts.length) {
      $("timeline-body").innerHTML =
        `<div class="empty">Load the demo memory to populate the semantic layer.</div>`;
      return;
    }

    $("timeline-body").innerHTML = facts
      .map((fact) => {
        const predicate = esc(fact.predicate.replace(/_/g, " "));
        const tag = fact.is_current
          ? '<span class="tag tag-coral">Current</span>'
          : '<span class="tag tag-quiet">Superseded</span>';
        const replaced = fact.superseded_by?.length
          ? `<span class="label">→ ${esc(fact.superseded_by.join(", "))}</span>`
          : "";
        const negated = fact.polarity === "negative" ? '<span class="label">negated</span>' : "";
        return `<div class="tl-row ${fact.is_current ? "tl-current" : ""}">
          <span class="data">${esc(fact.date)}</span>
          <span class="tl-fact">
            <span class="predicate">${esc(fact.subject)} · ${predicate}</span>
            <span class="object ${fact.is_current ? "" : "struck"}"> ${esc(fact.object)}</span>
            ${fact.qualifier ? `<span class="label"> for ${esc(fact.qualifier)}</span>` : ""}
          </span>
          <span class="row" style="gap:8px">${negated}${replaced}${tag}</span>
        </div>`;
      })
      .join("");
  }

  async function refreshConflicts() {
    const data = await api("/conflicts?limit=30");
    const conflicts = data.conflicts || [];
    const open = conflicts.filter((c) => c.status === "open").length;
    $("conflict-count").textContent = `${conflicts.length} TOTAL · ${open} OPEN`;

    if (!conflicts.length) {
      $("conflicts").innerHTML = `<div class="empty">No contradictions recorded yet.</div>`;
      return;
    }

    $("conflicts").innerHTML = conflicts
      .map((conflict) => {
        const values = (conflict.involved || [])
          .map((fact) => {
            const winner = fact.id === conflict.winner_id;
            return `<span class="tag ${winner ? "tag-signal" : "tag-quiet"}">${esc(
              fact.date
            )} · ${esc(fact.object)}${winner ? " · kept" : ""}</span>`;
          })
          .join(" ");
        return `<div class="quiet-card">
          <div class="spread" style="gap:12px">
            <span class="mono" style="font-size:14px">${esc(conflict.subject)} · ${esc(
          conflict.predicate.replace(/_/g, " ")
        )}</span>
            <span class="tag ${conflict.status === "resolved" ? "tag-quiet" : "tag-signal"}">${esc(
          conflict.status
        )} · ${esc(conflict.resolution_policy || "pending")}</span>
          </div>
          <div class="row" style="margin-top:12px;gap:8px">${values}</div>
        </div>`;
      })
      .join("");
  }

  // ------------------------------------------------------------------ graph

  const LAYER_X = { episodic: 150, semantic: 545, procedural: 940 };
  const LAYER_COLOR = { episodic: "#c9bfea", semantic: "#f2a683", procedural: "#b8e6a8" };

  async function refreshGraph() {
    const layer = $("graph-layer").value;
    const data = await api(`/graph?limit=150${layer ? `&layer=${layer}` : ""}`);
    const nodes = data.nodes || [];
    $("graph-count").textContent = `${nodes.length} nodes · ${(data.edges || []).length} edges`;

    if (!nodes.length) {
      $("graph-canvas").innerHTML =
        `<div style="padding:26px"><div class="empty">Load the demo memory to render the graph.</div></div>`;
      return;
    }

    const W = 1090;
    const H = 460;
    const columns = { episodic: [], semantic: [], procedural: [] };
    nodes.forEach((node) => (columns[node.layer] || columns.semantic).push(node));

    const position = new Map();
    Object.entries(columns).forEach(([name, list]) => {
      const x = LAYER_X[name];
      const span = H - 90;
      list.forEach((node, index) => {
        const step = list.length > 1 ? span / (list.length - 1) : 0;
        const y = list.length > 1 ? 52 + index * step : H / 2;
        const offset = ((index % 5) - 2) * 32;
        position.set(node.id, { x: x + offset, y });
      });
    });

    const edges = (data.edges || [])
      .filter((edge) => position.has(edge.source) && position.has(edge.target))
      .slice(0, 400)
      .map((edge) => {
        const a = position.get(edge.source);
        const b = position.get(edge.target);
        return `<path d="M${a.x} ${a.y} Q ${(a.x + b.x) / 2} ${(a.y + b.y) / 2} ${b.x} ${b.y}"
          fill="none" stroke="rgba(28,23,38,0.16)" stroke-width="1.25"/>`;
      })
      .join("");

    const circles = nodes
      .map((node) => {
        const point = position.get(node.id);
        if (!point) return "";
        const color = LAYER_COLOR[node.layer] || "#c9bfea";
        const superseded = node.is_current === false;
        const r = node.label === "Fact" || node.label === "Entity" ? 6 : 4.5;
        return `<g><title>${esc(node.label)} · ${esc(node.title)}</title>
          <circle cx="${point.x}" cy="${point.y}" r="${r}"
            fill="${superseded ? "#fdfbfe" : color}"
            stroke="#1c1726" stroke-width="${superseded ? 1.25 : 1.75}"
            ${superseded ? 'stroke-dasharray="2 2"' : ""}/></g>`;
      })
      .join("");

    const headers = Object.entries(LAYER_X)
      .map(
        ([name, x]) =>
          `<text x="${x}" y="26" text-anchor="middle" fill="#948da3"
             font-family="IBM Plex Mono, monospace" font-size="11"
             letter-spacing="1.4">${name.toUpperCase()}</text>`
      )
      .join("");

    $("graph-canvas").innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img"
      aria-label="Weave graph: ${nodes.length} nodes across three layers">
      ${headers}${edges}${circles}</svg>`;
  }

  // ---------------------------------------------------------------- routing

  async function refreshRouting() {
    const data = await api("/procedural");
    const routing = data.routing || [];
    const trained = routing.filter((row) => row.paths.length).length;
    $("routing-state").textContent = trained ? `${trained} TRAINED` : "AWAITING OUTCOMES";

    if (!routing.some((row) => row.paths.length)) {
      $("routing-body").innerHTML =
        `<div class="empty">Mark answers correct or incorrect to train the router.</div>`;
      return;
    }

    const rows = [];
    routing.forEach((entry) => {
      if (!entry.paths.length) {
        rows.push(`<tr>
          <td><span class="tag tag-quiet">${esc(entry.query_type)}</span></td>
          <td class="mono" style="font-size:13px;color:var(--color-muted-2)">${esc(
            entry.default_path
          )} · default</td>
          <td class="num">—</td><td class="num">—</td>
          <td><div class="meter"><span style="width:0"></span></div></td>
          <td class="num">—</td></tr>`);
        return;
      }
      entry.paths.forEach((path, index) => {
        rows.push(`<tr>
          <td>${index === 0 ? `<span class="tag tag-mint">${esc(entry.query_type)}</span>` : ""}</td>
          <td class="mono" style="font-size:13px">${esc(path.path)}${
            path.path === entry.default_path
              ? ' <span class="label">· default</span>'
              : ""
          }</td>
          <td class="num">${num(path.attempts)}</td>
          <td class="num">${num(path.successes)}</td>
          <td><div class="meter"><span style="width:${Math.round(
            path.success_rate * 100
          )}%"></span></div></td>
          <td class="num">${pct(path.success_rate)}</td></tr>`);
      });
    });

    $("routing-body").innerHTML = `<table class="routing">
      <thead><tr>
        <th>Query type</th><th>Path</th><th style="text-align:right">Tried</th>
        <th style="text-align:right">Won</th><th>Success</th><th style="text-align:right">Rate</th>
      </tr></thead><tbody>${rows.join("")}</tbody></table>`;
  }

  // ---------------------------------------------------------------- actions

  async function refreshAll() {
    await Promise.all([
      refreshStats(),
      refreshTimeline(),
      refreshConflicts(),
      refreshGraph(),
      refreshRouting(),
    ]);
  }

  async function seedDemo(button) {
    if (state.busy) return;
    state.busy = true;
    const original = button.textContent;
    button.disabled = true;
    button.innerHTML = '<span class="spinner"></span> Loading';
    try {
      const result = await api("/demo/seed", { method: "POST" });
      const conflicts = result.consolidation?.conflicts_resolved ?? 0;
      toast(`${result.ingested.length} sessions · ${conflicts} conflicts resolved`);
      await refreshAll();
    } catch (error) {
      toast(`Seed failed · ${error.message}`);
    } finally {
      state.busy = false;
      button.disabled = false;
      button.textContent = original;
    }
  }

  function bind() {
    $("query-form").addEventListener("submit", (event) => {
      event.preventDefault();
      runQuery();
    });
    $("btn-good").addEventListener("click", () => sendFeedback(true));
    $("btn-bad").addEventListener("click", () => sendFeedback(false));
    $("btn-seed").addEventListener("click", (e) => seedDemo(e.currentTarget));
    $("btn-seed-2").addEventListener("click", (e) => seedDemo(e.currentTarget));
    $("btn-graph").addEventListener("click", refreshGraph);
    $("graph-layer").addEventListener("change", refreshGraph);

    $("btn-consolidate").addEventListener("click", async (event) => {
      const button = event.currentTarget;
      button.disabled = true;
      try {
        const report = await api("/consolidate", {
          method: "POST",
          body: JSON.stringify({ policy: $("policy").value, max_conflicts: 100 }),
        });
        toast(
          `${report.conflicts_resolved} resolved · ${report.facts_superseded} superseded · ` +
            `${report.duplicates_merged} merged`
        );
        await refreshAll();
      } catch (error) {
        toast(`Consolidation failed · ${error.message}`);
      } finally {
        button.disabled = false;
      }
    });

    $("btn-reset").addEventListener("click", async (event) => {
      const button = event.currentTarget;
      button.disabled = true;
      try {
        await api("/reset", { method: "POST" });
        state.lastQuery = null;
        $("feedback-block").hidden = true;
        toast("Graph cleared");
        await refreshAll();
      } catch (error) {
        toast(`Reset failed · ${error.message}`);
      } finally {
        button.disabled = false;
      }
    });
  }

  async function boot() {
    renderSuggestions();
    bind();
    initParticles();
    initReveals();
    heroStagger();
    initThesis();
    await refreshHealth();
    try {
      await refreshAll();
    } catch (error) {
      toast(`Could not reach the API · ${error.message}`);
    }
  }

  document.addEventListener("DOMContentLoaded", boot);
})();
