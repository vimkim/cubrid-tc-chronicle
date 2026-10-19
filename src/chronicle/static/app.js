"use strict";
const $ = (id) => document.getElementById(id);
const number = (value) => new Intl.NumberFormat("en").format(value ?? 0);
const basename = (path) => path.split("/").pop();
const state = {
  suite: "",
  q: "",
  failedOnly: true,
  selected: null,
  cases: [],
  total: 0,
  coverage: null,
};
const pending = {};
function node(tag, attrs = {}, ...children) {
  const element = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key.startsWith("on")) element.addEventListener(key.slice(2), value);
    else if (value !== null && value !== undefined)
      element.setAttribute(key, value);
  }
  for (const child of children.flat())
    if (child !== null && child !== undefined)
      element.append(
        child instanceof Node ? child : document.createTextNode(String(child)),
      );
  return element;
}
function link(label, url, className = "") {
  try {
    if (!["http:", "https:"].includes(new URL(url, location.href).protocol))
      return node("span", {}, label);
  } catch {
    return node("span", {}, label);
  }
  return node(
    "a",
    {
      href: url,
      target: "_blank",
      rel: "noopener noreferrer",
      class: className,
    },
    label,
  );
}
function utc(value) {
  if (!value) return "Order unknown";
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? "Order unknown"
    : date.toLocaleString("en-GB", {
        timeZone: "UTC",
        day: "2-digit",
        month: "short",
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
      });
}
function pullRequest(pr, stacked = false) {
  if (!pr) return node("span", { class: "muted" }, "PR unknown · author unknown");
  const author = pr.author
    ? pr.author_url
      ? link(`@${pr.author}`, pr.author_url)
      : `@${pr.author}`
    : "unknown";
  return node(
    "span",
    { class: "pr-reference", title: pr.title || "Run-associated pull request" },
    link(`PR #${pr.number}`, pr.url),
    stacked ? null : " · ",
    node("span", { class: stacked ? "pr-author" : "" }, "Author: ", author),
  );
}
function reason(value) {
  return value.replaceAll("_", " ");
}
function badge(value) {
  return node(
    "span",
    { class: `outcome ${value}` },
    value.replaceAll("_", " "),
  );
}
function empty(title, description) {
  return node(
    "div",
    { class: "empty-state" },
    node("strong", {}, title),
    description,
  );
}
function showError(error) {
  if (error.name !== "AbortError") {
    $("error").textContent = error.message;
    $("error").hidden = false;
  }
}
async function request(channel, path, args = {}) {
  pending[channel]?.abort();
  const controller = new AbortController();
  pending[channel] = controller;
  const params = new URLSearchParams(
    Object.entries(args).filter(
      ([, value]) => value !== null && value !== undefined && value !== "",
    ),
  );
  const response = await fetch(`${path}?${params}`, {
    signal: controller.signal,
  });
  const result = await response.json();
  if (!response.ok) {
    const error = new Error(
      result.error || "The local index could not be read.",
    );
    error.candidates = result.candidates;
    throw error;
  }
  return result;
}
function writeURL() {
  const params = new URLSearchParams();
  if (state.q) params.set("q", state.q);
  if (state.suite) params.set("suite", state.suite);
  if (!state.failedOnly) params.set("all_cases", "true");
  if (state.selected) {
    params.set("case", state.selected.path);
    params.set("variant", state.selected.variant || "unknown");
    if (state.selected.execution) {
      params.set("execution_id", state.selected.execution);
      params.set("ordinal", state.selected.ordinal || 1);
    }
    for (const key of ["outcome", "engine", "since", "until"])
      if ($(key).value) params.set(key, $(key).value);
  }
  history.pushState(null, "", params.size ? `/?${params}` : "/");
}
function updateCoverage(coverage) {
  state.coverage = coverage;
  $("generation").textContent =
    `Index generation ${number(coverage.generation)}`;
  $("sync-status").textContent = coverage.last_sync
    ? `Last synchronized ${utc(coverage.last_sync)} UTC`
    : "Not synchronized yet";
  $("coverage-strip").replaceChildren(
    node(
      "span",
      { class: "coverage-label" },
      coverage.complete ? "● Complete selected window" : "◐ Partial history",
    ),
    node(
      "span",
      {},
      coverage.complete
        ? "All discoverable runs in this window are accounted for."
        : coverage.missing_runs.length
          ? `${number(coverage.missing_runs.length)} discovered runs are not indexed yet. Missing history stays unknown.`
          : "Earlier failures are useful. Missing history stays unknown.",
    ),
    node("button", { onclick: showCoverage }, "Inspect coverage →"),
  );
}
async function loadOverview() {
  try {
    const data = await request("overview", "/api/overview", {
      suite: state.suite,
    });
    const counts = data.counts;
    $("stats").replaceChildren(
      ...[
        [
          "RETAINED OUTCOMES",
          counts.observations,
          "Explicit case-level verdicts",
        ],
        ["INDEXED RUNS", counts.runs, "Including gated or skipped runs"],
        [
          "CASES WITH FAILURES",
          counts.failed_testcases,
          "Distinct testcase identities",
        ],
        [
          "FAILED ENGINE COMMITS",
          counts.failed_commits,
          "Revision-bearing evidence",
        ],
      ].map(([label, count, note]) =>
        node(
          "div",
          { class: "stat" },
          node("span", {}, label),
          node("strong", {}, number(count)),
          node("small", {}, note),
        ),
      ),
    );
    if (!state.selected) updateCoverage(data.coverage);
  } catch (error) {
    showError(error);
  }
}
function chooseCase(testcase, execution = null, ordinal = 1) {
  state.selected = {
    path: testcase.path,
    suite: testcase.suite,
    variant: testcase.variant,
    execution,
    ordinal,
  };
  state.suite = testcase.suite;
  for (const input of document.querySelectorAll('input[name="suite"]'))
    input.checked = input.value === state.suite;
  for (const key of ["outcome", "engine", "since", "until"]) $(key).value = "";
  writeURL();
  loadHistory();
  loadCatalog();
  loadOverview();
}
function renderCatalog() {
  $("case-count").textContent =
    `${number(state.total)} testcase${state.total === 1 ? "" : "s"}`;
  $("case-list").replaceChildren(
    ...state.cases.map((c) =>
      node(
        "button",
        {
          class: `case-button ${c.failures ? "" : "pass-case"} ${state.selected?.path === c.path && (state.selected.variant || null) === c.variant ? "active" : ""}`,
          onclick: () => chooseCase(c),
          title: c.path,
        },
        node(
          "span",
          { class: "case-button-title" },
          node("span", { class: "failure-dot" }),
          node("strong", {}, basename(c.path)),
        ),
        node(
          "span",
          { class: "case-location" },
          c.path.slice(0, c.path.lastIndexOf("/")),
        ),
        node(
          "p",
          {},
          `${number(c.failures)} failure snapshots · ${number(c.failed_commits)} engine commits`,
        ),
      ),
    ),
  );
  $("more-cases").hidden = state.cases.length >= state.total;
  $("catalog-title").textContent = state.q
    ? "Matching testcase histories"
    : state.failedOnly
      ? "Recent testcase failures"
      : "Recent testcase histories";
  if (!state.cases.length) {
    $("home-cases").replaceChildren(
      empty(
        "No retained testcase matches",
        "Try another path, filename, ticket, or suite. Clear the failure-only filter to include passing cases.",
      ),
    );
    return;
  }
  const table = node(
    "table",
    {},
    node(
      "thead",
      {},
      node(
        "tr",
        {},
        ...[
          "TESTCASE",
          "SUITE",
          "FAILURES",
          "ENGINE COMMITS",
          "LAST OBSERVED UTC",
        ].map((t) => node("th", { scope: "col" }, t)),
      ),
    ),
  );
  const body = node("tbody");
  for (const c of state.cases)
    body.append(
      node(
        "tr",
        {},
        node(
          "td",
          {},
          node(
            "button",
            { class: "table-case-button", onclick: () => chooseCase(c) },
            basename(c.path),
          ),
          node("span", { class: "table-path", title: c.path }, c.path),
        ),
        node("td", {}, node("span", { class: "suite-badge" }, c.suite)),
        node("td", {}, number(c.failures)),
        node("td", {}, number(c.failed_commits)),
        node("td", { class: "muted" }, utc(c.last_completed_at)),
      ),
    );
  table.append(body);
  $("home-cases").replaceChildren(node("div", { class: "table-wrap" }, table));
  if (state.cases.length < state.total)
    $("home-cases").append(
      node(
        "button",
        { class: "more-history", onclick: () => loadCatalog(true) },
        "Load more testcases",
      ),
    );
}
async function loadCatalog(more = false) {
  try {
    const data = await request("catalog", "/api/cases", {
      q: state.q,
      suite: state.suite,
      failed_only: String(state.failedOnly),
      offset: more ? state.cases.length : 0,
    });
    state.cases = more ? [...state.cases, ...data.cases] : data.cases;
    state.total = data.total;
    renderCatalog();
  } catch (error) {
    showError(error);
  }
}
function showCoverage() {
  const c = state.coverage;
  if (!c) return;
  const grid = node("dl", { class: "fact-grid" });
  for (const [label, value] of [
    ["Enumerated runs", number(c.enumerated_runs)],
    ["Retained snapshots", number(c.executions)],
    ["Complete result snapshots", number(c.complete_results)],
    ["Partial result snapshots", number(c.partial_results)],
    ["Unresolved attempts", number(c.unresolved_attempts)],
    ["Unresolved revisions", number(c.unresolved_revisions)],
    ["Missing run metadata", number(c.missing_runs.length)],
    ["Missing shard snapshots", number(c.missing_shards.length)],
  ])
    grid.append(node("div", {}, node("dt", {}, label), node("dd", {}, value)));
  const reasons = node(
    "ul",
    { class: "reason-list" },
    c.reasons.map((r) => node("li", {}, reason(r))),
  );
  const gaps = node("ul", { class: "gap-list" });
  for (const g of c.gaps.slice(0, 40))
    gaps.append(
      node(
        "li",
        {},
        `Run ${g.run_id} / ${g.suite} / ${g.location}: ${reason(g.reason)}`,
      ),
    );
  if (c.gaps.length > 40)
    gaps.append(
      node(
        "li",
        {},
        `And ${number(c.gaps.length - 40)} more unavailable source files.`,
      ),
    );
  $("coverage-content").replaceChildren(
    node(
      "p",
      { class: "dialog-summary" },
      `${c.window.since || "Unknown start"} through ${c.window.until || "unknown end"} UTC. Window selection uses run creation time. Discovery is ${c.discovery_complete ? "complete" : "incomplete"} for this interval.`,
    ),
    grid,
    node(
      "h3",
      {},
      c.reasons.length ? "Coverage gaps" : "No known gaps in this window",
    ),
    reasons,
    gaps,
    node(
      "p",
      { class: "dialog-note" },
      `Excluded sources: ${c.excluded_sources.join(", ")}. ${c.upstream_limit}`,
    ),
    node(
      "p",
      { class: "dialog-note" },
      `Earliest retained execution: ${c.earliest_retained_execution || "unknown"}. Index generation ${c.generation}.`,
    ),
  );
  $("coverage-dialog").showModal();
}
async function showEvidence(execution) {
  $("evidence-content").replaceChildren(
    node("p", { class: "loading" }, "Reading retained evidence…"),
  );
  $("evidence-dialog").showModal();
  try {
    const data = await request("evidence", "/api/evidence", { execution });
    const list = node("div", { class: "source-files" });
    for (const f of data.files) {
      const actions = node(
        "div",
        { class: "source-file-links" },
        link(
          "Retained bytes ↗",
          `/api/evidence?${new URLSearchParams({ execution, file: f.name })}`,
        ),
      );
      if (f.source) actions.append(link("Original source ↗", f.source));
      list.append(
        node(
          "div",
          { class: "source-file" },
          node(
            "div",
            {},
            node("strong", {}, f.name),
            node("small", {}, `${number(f.bytes)} bytes`),
          ),
          actions,
        ),
      );
    }
    $("evidence-content").replaceChildren(
      node(
        "p",
        { class: "dialog-summary" },
        `Run ${data.run_id} · ${data.suite} shard ${data.shard} · snapshot ${execution}.`,
      ),
      list,
      node(
        "p",
        { class: "dialog-note" },
        "Retained bytes are checksum verified. Original source URLs can change after reruns. The shared result directory does not establish a testcase execution attempt.",
      ),
    );
  } catch (error) {
    $("evidence-content").replaceChildren(
      empty("Evidence unavailable", error.message),
    );
  }
}
function renderHistory(report) {
  updateCoverage(report.coverage);
  const target = report.target;
  const prior = report.prior_failures;
  const status = report.history_result;
  const noFailures = !report.occurrences.some((o) =>
    ["FAIL", "ERROR"].includes(o.outcome),
  );
  const title = noFailures
    ? "No recorded failures in this result"
    : status === "SEEN_BEFORE"
      ? "This testcase has failed before."
      : status === "FIRST_OBSERVED_IN_WINDOW"
        ? "First observed failure in this window."
        : "Earlier history is uncertain.";
  const detail =
    status === "SEEN_BEFORE"
      ? `${number(prior.length)} earlier failure snapshot${prior.length === 1 ? "" : "s"} across ${number(new Set(prior.map((o) => o.engine_sha).filter(Boolean)).size)} verified engine commits. Testcase revision and execution context may differ.`
      : status === "FIRST_OBSERVED_IN_WINDOW"
        ? "No earlier indexed failure matches within the complete selected window. This does not identify the introducing commit."
        : noFailures
          ? "No case-level failure matches the current filters. Missing or uncollected outcomes remain unknown."
          : "No earlier failure can be established from the available ordering and coverage. The retained evidence cannot establish novelty.";
  const card = node(
    "div",
    { class: `verdict ${status === "UNKNOWN_HISTORY" ? "unknown" : ""}` },
    node(
      "div",
      { class: "verdict-icon", "aria-hidden": "true" },
      status === "SEEN_BEFORE" ? "↶" : status === "UNKNOWN_HISTORY" ? "?" : "◎",
    ),
    node(
      "div",
      {},
      node("h3", {}, title),
      node("p", {}, detail),
      target
        ? node(
            "p",
            { class: "target-label" },
            node(
              "strong",
              {},
              `Target: run ${target.run_id} · snapshot ${target.id}`,
            ),
            ` · ${target.engine_sha?.slice(0, 12) || "unknown engine"} · ${report.target_selection === "explicit" ? "selected failure" : "latest indexed failure"}`,
          )
        : null,
      target
        ? node("p", { class: "target-pr" }, pullRequest(target.pull_request))
        : null,
    ),
  );
  const meta = node(
    "div",
    { class: "history-meta" },
    node(
      "span",
      {},
      node("strong", {}, number(report.occurrences.length)),
      " retained outcome snapshots",
    ),
    node(
      "span",
      {},
      node("strong", {}, number(report.failed_commits.length)),
      " failed engine commits",
    ),
    node(
      "span",
      {},
      node("strong", {}, number(report.prior_verified_executions)),
      " earlier ordered outcomes",
    ),
  );
  const commits = node("div", { class: "commits" });
  for (const sha of report.failed_commits)
    commits.append(
      node(
        "button",
        {
          class: "commit-chip",
          title: `Filter engine ${sha}`,
          onclick: () => {
            $("engine").value = sha;
            state.selected.execution = null;
            writeURL();
            loadHistory();
          },
        },
        sha.slice(0, 12),
        node(
          "span",
          {},
          `${number(report.occurrences.filter((o) => o.engine_sha === sha && ["FAIL", "ERROR"].includes(o.outcome)).length)} failure snapshots`,
        ),
      ),
    );
  const rows = [...report.occurrences].reverse();
  const strip = node("div", {
    class: "timeline-strip",
    "aria-label": "Recent observed outcomes, oldest to newest",
  });
  for (const o of report.occurrences.slice(-40))
    strip.append(
      node("button", {
        class: `timeline-dot ${o.outcome} ${o.id === target?.id && o.ordinal === target?.ordinal ? "target" : ""}`,
        title: `${utc(o.completed_at)} UTC · ${o.outcome} · run ${o.run_id}`,
        "aria-label": `${o.outcome} in run ${o.run_id}, ${utc(o.completed_at)} UTC`,
        onclick: () => showEvidence(o.id),
      }),
    );
  const panel = node(
    "div",
    { class: "panel" },
    node(
      "div",
      { class: "panel-heading" },
      node(
        "div",
        {},
        node("p", { class: "eyebrow" }, "RETAINED EXECUTION EVIDENCE"),
        node("h2", {}, "Observed timeline"),
        strip,
      ),
      node("span", { class: "muted" }, "Latest first · UTC"),
    ),
  );
  const table = node(
    "table",
    { class: "history-table" },
    node(
      "thead",
      {},
      node(
        "tr",
        {},
        ...[
          "COMPLETED UTC",
          "ENGINE COMMIT",
          "TESTCASE REVISION",
          "OUTCOME",
          "PR / AUTHOR",
          "RUN / SNAPSHOT",
          "EVIDENCE",
        ].map((t) => node("th", { scope: "col" }, t)),
      ),
    ),
  );
  const body = node("tbody");
  table.append(body);
  let visible = 0;
  const pageSize = matchMedia("(max-width: 760px)").matches ? 20 : 50;
  function appendRows() {
    for (const o of rows.slice(visible, visible + pageSize)) {
      const isTarget = o.id === target?.id && o.ordinal === target?.ordinal;
      const actions = node(
        "div",
        { class: "row-actions" },
        node(
          "button",
          {
            class: "link-button",
            onclick: () => showEvidence(o.id),
            "aria-label": `View evidence for snapshot ${o.id}`,
          },
          "Files ↗",
        ),
      );
      if (o.source) actions.append(link("Source ↗", o.source));
      const runCell = node(
        "td",
        { "data-label": "Run / snapshot" },
        o.metadata.run_url
          ? link(String(o.run_id), o.metadata.run_url)
          : String(o.run_id),
        node(
          "span",
          { class: "table-path" },
          `Snapshot ${o.id} · shard ${o.shard} · attempt ${o.attempt ?? "unknown"}`,
        ),
      );
      if (!isTarget && ["FAIL", "ERROR"].includes(o.outcome))
        runCell.append(
          node(
            "button",
            {
              class: "link-button",
              onclick: () => {
                state.selected.execution = o.id;
                state.selected.ordinal = o.ordinal;
                writeURL();
                loadHistory();
              },
            },
            "Use as target",
          ),
        );
      body.append(
        node(
          "tr",
          { class: isTarget ? "target-row" : "" },
          node(
            "td",
            {
              title: o.completed_at || "Execution ordering unavailable",
              "data-label": "Completed UTC",
            },
            utc(o.completed_at),
            isTarget
              ? node("span", { class: "target-marker" }, "SELECTED TARGET")
              : null,
          ),
          node(
            "td",
            {
              class: "mono",
              title: o.engine_sha || "Revision unresolved",
              "data-label": "Engine commit",
            },
            o.engine_sha
              ? link(
                  o.engine_sha.slice(0, 12),
                  `https://github.com/${report.coverage.repository}/commit/${o.engine_sha}`,
                )
              : "Unknown",
          ),
          node(
            "td",
            {
              class: "mono",
              title: o.testcase_sha || "Revision unresolved",
              "data-label": "Testcase revision",
            },
            o.testcase_sha
              ? link(
                  o.testcase_sha.slice(0, 12),
                  `https://github.com/${report.identity.repository}/blob/${o.testcase_sha}/${report.identity.path.split("/").map(encodeURIComponent).join("/")}`,
                )
              : "Unknown",
          ),
          node("td", {}, badge(o.outcome)),
          node("td", { "data-label": "PR / author" }, pullRequest(o.pull_request, true)),
          runCell,
          node("td", { "data-label": "Evidence" }, actions),
        ),
      );
    }
    visible += pageSize;
    more.hidden = visible >= rows.length;
  }
  const more = node(
    "button",
    { class: "more-history", onclick: appendRows },
    "Show more observations",
  );
  if (rows.length) {
    panel.append(node("div", { class: "table-wrap" }, table), more);
    appendRows();
  } else
    panel.append(
      empty(
        "No matching retained outcomes",
        "Reset the filters or inspect the coverage gaps.",
      ),
    );
  const unknownContext =
    target &&
    ["ctp_sha", "platform", "configuration_digest"].some(
      (k) => !target.metadata[k],
    );
  panel.append(
    node(
      "p",
      { class: "history-notes" },
      `${unknownContext ? "Tooling, platform, or configuration is unknown. " : ""}A matching path establishes testcase recurrence. It does not establish a shared cause. Snapshot counts can include reruns or unresolved invocations.`,
    ),
  );
  $("history-content").replaceChildren(card, meta, commits, panel);
}
async function loadHistory() {
  if (!state.selected) return;
  $("error").hidden = true;
  $("home").hidden = true;
  $("detail").hidden = false;
  $("case-name").textContent = basename(state.selected.path);
  $("case-path").textContent = state.selected.path;
  $("case-suite").textContent =
    state.selected.suite || state.suite || "TESTCASE";
  $("history-content").replaceChildren(
    node("p", { class: "loading" }, "Reading retained testcase history…"),
  );
  try {
    const report = await request("history", "/api/history", {
      case: state.selected.path,
      suite: state.selected.suite || state.suite,
      variant: state.selected.variant || "unknown",
      execution_id: state.selected.execution,
      ordinal: state.selected.execution ? state.selected.ordinal : null,
      outcome: $("outcome").value,
      engine_sha: $("engine").value.trim(),
      since: $("since").value,
      until: $("until").value,
    });
    renderHistory(report);
  } catch (error) {
    if (error.name === "AbortError") return;
    if (error.candidates)
      $("history-content").replaceChildren(
        empty(
          error.candidates.some((c) => c.path)
            ? "Choose a testcase"
            : "Choose an occurrence",
          "More than one identity or failure matches. Select an exact match below.",
        ),
        ...error.candidates.map((c) =>
          node(
            "button",
            {
              class: "commit-chip",
              onclick: () => {
                if (c.path) {
                  chooseCase(c);
                  return;
                }
                state.selected.execution = c.execution_id;
                state.selected.ordinal = c.ordinal;
                writeURL();
                loadHistory();
              },
            },
            c.path
              ? `${c.suite} / ${c.path} · ${c.variant || "unknown variant"}`
              : `Snapshot ${c.execution_id} · invocation ${c.ordinal}`,
          ),
        ),
      );
    else {
      $("history-content").replaceChildren(
        empty("History could not be read", error.message),
      );
      showError(error);
    }
  }
}
function clearSelection() {
  pending.history?.abort();
  state.selected = null;
  $("detail").hidden = true;
  $("home").hidden = false;
}
function fromURL() {
  const params = new URLSearchParams(location.search);
  state.q = params.get("q") || "";
  state.suite = params.get("suite") || "";
  state.failedOnly = params.get("all_cases") !== "true";
  $("search").value = state.q;
  $("failed-only").checked = state.failedOnly;
  for (const radio of document.querySelectorAll('input[name="suite"]'))
    radio.checked = radio.value === state.suite;
  for (const key of ["outcome", "engine", "since", "until"])
    $(key).value = params.get(key) || "";
  if (params.has("case")) {
    state.selected = {
      path: params.get("case"),
      suite: state.suite,
      variant:
        params.get("variant") === "unknown" ? null : params.get("variant"),
      execution: params.get("execution_id"),
      ordinal: params.get("ordinal") || 1,
    };
    loadHistory();
  } else clearSelection();
  loadOverview();
  loadCatalog();
}
let timer;
$("search").addEventListener("input", () => {
  clearTimeout(timer);
  timer = setTimeout(() => {
    state.q = $("search").value.trim();
    writeURL();
    loadCatalog();
  }, 250);
});
$("search-form").addEventListener("submit", (event) => {
  event.preventDefault();
  clearTimeout(timer);
  state.q = $("search").value.trim();
  writeURL();
  loadCatalog();
});
document.querySelectorAll('input[name="suite"]').forEach((input) =>
  input.addEventListener("change", () => {
    state.suite = input.value;
    clearSelection();
    writeURL();
    loadOverview();
    loadCatalog();
  }),
);
$("failed-only").addEventListener("change", () => {
  state.failedOnly = $("failed-only").checked;
  writeURL();
  loadCatalog();
});
$("more-cases").addEventListener("click", () => loadCatalog(true));
$("coverage-button").addEventListener("click", showCoverage);
$("clear-selection").addEventListener("click", () => {
  clearSelection();
  writeURL();
  loadOverview();
  loadCatalog();
});
$("filters").addEventListener("submit", (event) => {
  event.preventDefault();
  state.selected.execution = null;
  writeURL();
  loadHistory();
});
$("reset-filters").addEventListener("click", () => {
  for (const key of ["outcome", "engine", "since", "until"]) $(key).value = "";
  state.selected.execution = null;
  writeURL();
  loadHistory();
});
$("refresh").addEventListener("click", () => {
  $("error").hidden = true;
  loadOverview();
  loadCatalog();
  if (state.selected) loadHistory();
});
document
  .querySelectorAll(".close-dialog")
  .forEach((button) =>
    button.addEventListener("click", () => button.closest("dialog").close()),
  );
window.addEventListener("popstate", fromURL);
fromURL();
