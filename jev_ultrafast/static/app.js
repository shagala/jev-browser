const $ = (id) => document.getElementById(id);
const token = document.querySelector('meta[name="demo-token"]').content;
let state = null,
  busy = false,
  automatic = false;
const goals = {
  flights: 'Find one-way flights from Zurich to London on September 20, 2026, for one adult in economy. Stop when matching flight options are visible. Do not select or book a flight.',
  travel: 'Find a Design stay in Lisbon with Free cancellation and open Casa Flora.',
  research:
    "Open the article about using finite choices to control browser agents.",
};
const escape = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const percent = (value) => `${(value * 100).toFixed(value < 0.01 ? 1 : 0)}%`;
async function call(name, body = {}) {
  const response = await fetch(`/api/${name}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Demo-Token": token },
    body: JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) throw Error(data.error || "Request failed");
  state = data;
  render();
  return data;
}
function controls() {
  const live = state?.page && !["done", "blocked"].includes(state.status);
  $("start").disabled = busy;
  $("scenario").disabled = busy;
  $("goal").disabled = busy;
  $("choose").disabled = busy || !live;
  $("execute").disabled = busy || !state?.decision || !live;
  $("auto").disabled = busy || !live;
  $("auto").hidden = automatic;
  $("stop").hidden = !automatic;
  $("download").disabled = !state?.history?.length;
}
async function perform(fn, label) {
  if (busy) return;
  busy = true;
  $("error").hidden = true;
  controls();
  $("status").textContent = label;
  try {
    await fn();
  } catch (error) {
    automatic = false;
    try {
      state = await fetch("/api/state").then((r) => r.json());
      render();
    } catch {
      /* Preserve the original failure if the server disconnected. */
    }
    $("error").textContent = error.message;
    $("error").hidden = false;
    $("status").textContent = "Paused · needs attention";
  } finally {
    busy = false;
    controls();
  }
}
function render() {
  if (!state) return;
  $("helper").textContent = `Text helper · ${state.text_model}`;
  $("plan").innerHTML = (state.plan || [])
    .map(
      (goal, i) =>
        `<div class="plan-step ${i === state.plan_index ? "current" : ""}"><span>${i < state.plan_index ? "✓" : i + 1}</span>${escape(goal)}</div>`,
    )
    .join("");
  const page = state.page,
    d =
      state.decision ||
      (state.status === "done" ? state.decisions?.at(-1) : null);
  const labels = {
    idle: "Ready to explore",
    ready: "Page observed · ready for a decision",
    predicted: "Choice ready · inspect or execute",
    done: "Jev reports complete · inspect the page",
    blocked: "Stopped · no supported next action",
  };
  $("status").textContent = labels[state.status] || state.status;
  if (!page) {
    controls();
    return;
  }
  $("empty").hidden = true;
  $("screenshot").hidden = false;
  $("screenshot").src = `data:image/jpeg;base64,${page.screenshot}`;
  $("url").textContent = page.url;
  $("page-title").textContent = page.title;
  $("action-count").textContent = `${state.elements.length} elements`;
  const chosen = page.actions.find((a) => a.id === d?.choice);
  $("choice-title").textContent = d
    ? chosen?.label || d.choice
    : "Choose an action";
  $("latency").textContent = d ? `${d.latency_ms} ms` : "—";
  $("confidence").textContent = d?.target_confidence != null ? percent(d.target_confidence) : "—";
  $("completion").textContent = d ? d.operation : "—";
  $("ranking-note").textContent = d ? "Ranked by Jev" : "Unranked";
  const op = Object.entries(d?.operation_probabilities || {}).sort((a,b)=>b[1]-a[1]);
  $("operation-choices").innerHTML = op.map(([name,p]) =>
    `<span class="operation-choice ${name === d.operation ? 'best' : ''}">${escape(name)} <b>${percent(p)}</b></span>`).join('');
  const probability = e => d?.target_probabilities[e.index] ??
    Math.max(-1, ...(e.options || []).map(o=>d?.target_probabilities[o.index] ?? -1));
  const selectedIndex = d?.target?.split(':')[0];
  const elements = [...state.elements];
  if (d) elements.sort((a,b)=>probability(b)-probability(a));
  $("choices").innerHTML = elements.map(e => {
    const p = probability(e);
    return `<div class="choice ${selectedIndex === e.index ? 'best' : ''}" data-action="${escape(e.index)}"><span class="choice-id">[${escape(e.index)}]</span><div class="choice-label">${escape(e.label)}<small>${escape(e.role)} · ${escape(e.operations.join(' / '))}${e.value ? ' · '+escape(e.value) : ''}${e.checked !== undefined ? ' · checked '+escape(e.checked) : ''}</small>${p >= 0 ? `<div class="bar" style="--probability:${p*100}%"></div>` : ''}</div><span class="probability">${p >= 0 ? percent(p) : '—'}</span></div>`;
  }).join('');
  const targets = new Map();
  for (const a of page.actions) if (a.rect && !targets.has(a.node)) targets.set(a.node, a);
  $("targets").innerHTML = [...targets.values()].map((a,i) => {
    const index=String(i+1);
    return `<div class="target ${index === selectedIndex ? 'selected' : ''}" data-action="${index}" style="left:${100*a.rect.x/page.w}%;top:${100*a.rect.y/page.h}%;width:${100*a.rect.w/page.w}%;height:${100*a.rect.h/page.h}%"><span>${index}</span></div>`;
  }).join('');
  $("targets").hidden = !$("overlays").checked;
  $("history").innerHTML = state.history.length
    ? state.history
        .map(
          (h) =>
            `<div class="trace-row"><span class="number">${String(h.step).padStart(2, "0")}</span><div>${escape(h.action)}${h.text ? ` <b>“${escape(h.text)}”</b><small>${escape(h.text_helper)}</small>` : ""}</div><span class="time">${h.latency_ms} ms · ${percent(h.probability)}</span><span class="effect">${h.page_changed ? "Page changed" : "No change observed"}</span></div>`,
        )
        .join("")
    : '<p class="muted">Each executed action leaves an observed result.</p>';
  $("step-count").textContent = `${state.history.length} actions · ${(state.elapsed_ms / 1000).toFixed(2)} s`;
  $("model-state").textContent = JSON.stringify(
    d?.request || {
      goal: state.goal,
      url: page.url,
      text: page.text,
      actions: page.actions.map(({ rect, node, ...rest }) => rest),
    },
    null,
    2,
  );
  controls();
}
$("task-form").addEventListener("submit", (event) => {
  event.preventDefault();
  automatic = false;
  perform(
    () =>
      call("reset", {
        scenario: $("scenario").value,
        goal: $("goal").value,
        url: $("url").value,
      }),
    "Opening a fresh browser…",
  );
});
$("scenario").addEventListener("change", () => {
  const custom = $("scenario").value === "custom";
  $("custom-url").hidden = !custom;
  if (!custom) $("goal").value = goals[$("scenario").value];
});
$("choose").addEventListener("click", () =>
  perform(() => call("predict"), "Jev is comparing the actions…"),
);
$("execute").addEventListener("click", () =>
  perform(
    () => call("act", { fingerprint: state.page.fingerprint }),
    "Executing the choice…",
  ),
);
$("auto").addEventListener("click", () =>
  perform(async () => {
    automatic = true;
    controls();
    for (let i = 0; i < state.max_steps * 2 && automatic; i++) {
      $("status").textContent = "Running…";
      if ($("pace").checked) {
        await call("predict");
        await new Promise(resolve => setTimeout(resolve, 450));
        if (!automatic) break;
        await call("act", {fingerprint: state.page.fingerprint});
      } else {
        await call("tick");
      }
      if (["done", "blocked"].includes(state.status)) break;
    }
    automatic = false;
  }, "Running the browser…"),
);
$("stop").addEventListener("click", () => {
  automatic = false;
  $("status").textContent = "Pausing after the current request…";
  controls();
});
$("overlays").addEventListener("change", () => {
  $("targets").hidden = !$("overlays").checked;
});
$("choices").addEventListener("pointerover", (event) => {
  const id = event.target.closest("[data-action]")?.dataset.action;
  document
    .querySelectorAll(".target")
    .forEach((t) =>
      t.classList.toggle(
        "selected",
        t.dataset.action === id || t.dataset.action === state?.decision?.target?.split(':')[0],
      ),
    );
});
$("choices").addEventListener("pointerleave", () =>
  document
    .querySelectorAll(".target")
    .forEach((t) =>
      t.classList.toggle(
        "selected",
        t.dataset.action === state?.decision?.target?.split(':')[0],
      ),
    ),
);
$("download").addEventListener("click", () => {
  const { page, ...rest } = state;
  const blob = new Blob(
    [
      JSON.stringify(
        { ...rest, page: { ...page, screenshot: undefined } },
        null,
        2,
      ),
    ],
    { type: "application/json" },
  );
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "typesafe-browser-trace.json";
  a.click();
  URL.revokeObjectURL(url);
});
fetch("/api/state")
  .then((r) => r.json())
  .then((s) => {
    state = s;
    render();
  })
  .catch(() => {
    $("status").textContent = "Cannot reach local demo server";
  });

// --- the recipe catalog ----------------------------------------------------------------
// Replay makes no decisions, so the panels above have nothing to show for it. Its view is
// the step ladder: what each step looked for, whether it acted, and what came back.
let recipePoll = null;

function recipeRow(recipe) {
  const last = recipe.last_ok
    ? `last ok ${escape(recipe.last_ok.replace("T", " ").slice(0, 16))}`
    : "never run";
  const steps = recipe.steps
    .map((s) => `<li><code>${escape(s.id)}</code> ${escape(s.op)} · ${escape(s.label ?? s.intent)}</li>`)
    .join("");
  return `<article class="recipe" data-recipe="${escape(recipe.name)}">
      <div class="recipe-head">
        <h3>${escape(recipe.name)} <span>v${recipe.version}</span></h3>
        <div class="recipe-actions">
          <button type="button" data-run="replay">Replay</button>
          <button type="button" data-run="run">Run with repair</button>
        </div>
      </div>
      <p class="recipe-meta">${escape(recipe.schedule)} · ${recipe.steps.length} step(s) · ${escape(last)}${
        recipe.fans_out ? " · fans out" : ""
      }</p>
      <ol class="recipe-steps">${steps}</ol>
      <details class="recipe-goal"><summary>Recorded from this goal</summary><p>${escape(
        recipe.goal,
      )}</p></details>
    </article>`;
}

async function loadRecipes() {
  let data;
  try {
    data = await fetch("/api/recipes").then((r) => r.json());
  } catch {
    return;
  }
  if (!data.available) {
    $("recipe-count").textContent = "unavailable";
    $("recipe-note").textContent = `No catalog here: ${data.reason}`;
    return;
  }
  $("recipe-count").textContent = `${data.recipes.length} recorded`;
  $("recipe-list").innerHTML =
    data.recipes.map(recipeRow).join("") ||
    `<p class="muted">Nothing recorded yet. Use <code>python -m usecases.run record</code>.</p>`;
}

function renderRecipeRun(run) {
  $("recipe-trace").hidden = false;
  $("recipe-status").textContent = run.running
    ? `${run.recipe} · running`
    : `${run.recipe} · ${run.status}`;
  $("ladder").querySelector("tbody").innerHTML = run.events
    .map(
      (e) =>
        `<tr class="stage-${escape(e.stage)}"><td>${escape(e.stage)}</td><td>${escape(
          e.step ?? "",
        )}</td><td>${escape(e.detail)}</td></tr>`,
    )
    .join("");
  $("recipe-results").innerHTML = run.results
    .map(
      (r) =>
        `<div class="recipe-result ${r.status}"><strong>${escape(r.status)}</strong> ${escape(
          Object.entries(r.params)
            .map(([k, v]) => `${k}=${v}`)
            .join(" "),
        )} · ${r.ms} ms${r.reason ? ` · ${escape(r.reason)}` : ""}</div>`,
    )
    .join("");
  const rows = $("ladder").querySelectorAll("tbody tr");
  if (rows.length) rows[rows.length - 1].scrollIntoView({ block: "nearest" });
}

async function pollRecipeRun() {
  const run = await fetch("/api/recipes/state").then((r) => r.json());
  renderRecipeRun(run);
  if (!run.running) {
    clearInterval(recipePoll);
    recipePoll = null;
    document.querySelectorAll("[data-run]").forEach((b) => (b.disabled = false));
    loadRecipes();
  }
}

$("recipe-list").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-run]");
  if (!button || recipePoll) return;
  const name = button.closest("[data-recipe]").dataset.recipe;
  document.querySelectorAll("[data-run]").forEach((b) => (b.disabled = true));
  $("error").hidden = true;
  try {
    const response = await fetch("/api/recipes/run", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Demo-Token": token },
      body: JSON.stringify({ name, mode: button.dataset.run }),
    });
    const data = await response.json();
    if (!response.ok) throw Error(data.error || "Request failed");
    renderRecipeRun(data);
    recipePoll = setInterval(pollRecipeRun, 500);
  } catch (error) {
    document.querySelectorAll("[data-run]").forEach((b) => (b.disabled = false));
    $("error").textContent = error.message;
    $("error").hidden = false;
  }
});

loadRecipes();
fetch("/api/recipes/state")
  .then((r) => r.json())
  .then((run) => {
    if (run.status === "idle") return;
    renderRecipeRun(run);
    if (run.running) recipePoll = setInterval(pollRecipeRun, 500);
  })
  .catch(() => {});
