/*
 * ReadyKit Edge operator console.
 *
 * Renders whatever the host reports and nothing else. In particular it never
 * infers a latch state from a verdict - it shows the latch the actuator node
 * actually reports, because a PASS whose RELEASE was never acknowledged did
 * not open anything, and a console that implied otherwise would be lying to
 * someone about to reach into an enclosure.
 */

const GLYPH = {
  found: "+",
  absent: "−",
  damaged: "!",
  unreadable: "?",
  unreported: "?",
};

const VERDICT_WORD = {
  pass: "PASS",
  fail: "FAIL",
  indeterminate: "INDETERMINATE",
};

const $ = (id) => document.getElementById(id);

let manifest = null;
let scenes = [];
let source = { live: false, source: "scripted scenes", engine: "simulated model" };
let busy = false;

async function getJSON(url, options) {
  const response = await fetch(url, options);
  if (!response.ok) throw new Error(`${url} returned ${response.status}`);
  return response.json();
}

/* ------------------------------------------------------------------ verdict */

function renderVerdict(last, telemetry) {
  const panel = $("verdict");
  const meta = $("verdict-meta");
  const warn = $("verdict-warn");

  if (!last) {
    panel.dataset.verdict = "none";
    meta.hidden = true;
    warn.innerHTML = "";
    return;
  }

  panel.dataset.verdict = last.verdict;
  $("verdict-word").textContent = VERDICT_WORD[last.verdict] || "UNKNOWN";
  $("verdict-reason").textContent = last.reason;

  /* The latch pill shows the latch NOW, not the latch at the moment of the
   * verdict. A five-second hold expires while this banner is still on screen,
   * and a banner reading "released" over telemetry reading "engaged" is the
   * one contradiction this console must never show - someone reads it before
   * reaching into an enclosure. Live telemetry wins; what the verdict
   * commanded is in the meta row. */
  const latch = $("verdict-latch");
  const live =
    telemetry && telemetry.available ? telemetry.latch : last.latch_expected;
  latch.dataset.latch = live;
  latch.textContent = `latch ${live}`;

  meta.hidden = false;
  $("m-engine").textContent = last.engine;
  $("m-latency").textContent = `${last.latency_ms}ms`;
  $("m-frame").textContent = last.frame_digest || "—";
  $("m-command").textContent = last.commanded;

  /* A verdict that was decided but never enacted is the case most likely to
   * mislead an operator, so it is called out rather than left in the meta row. */
  warn.innerHTML = "";
  if (!last.enacted) {
    const box = document.createElement("div");
    box.className = "warn";
    box.textContent =
      "The actuator node did not acknowledge this command. Nothing was " +
      "actuated — treat the enclosure as unchanged.";
    warn.appendChild(box);
  }

  renderBlueprint(last);
}

/* --------------------------------------------------------------- blueprint */

/* The same model reply, run through the original blueprint's substring matcher.
 * Displayed and recorded only - it is never sent to the actuator node. */
function renderBlueprint(last) {
  const host = $("blueprint");
  host.innerHTML = "";

  const bp = last.blueprint;
  if (!bp) return;

  const box = document.createElement("div");
  box.className = "compare";
  box.dataset.divergence = bp.divergence;

  const head = document.createElement("div");
  head.className = "compare__head";

  const title = document.createElement("span");
  title.className = "compare__title";
  title.textContent = "Original blueprint logic";

  const tag = document.createElement("span");
  tag.className = "compare__tag";
  tag.textContent =
    bp.divergence === "unsafe"
      ? "would have unlocked"
      : bp.divergence === "spurious"
        ? "would have rejected a good kit"
        : "same decision";

  const signal = document.createElement("code");
  signal.className = "compare__signal";
  signal.textContent = bp.signal;

  head.append(title, tag, signal);

  const why = document.createElement("p");
  why.className = "compare__why";
  why.textContent = `${bp.reason}.`;

  box.append(head, why);

  /* The verbatim first line is the point: it is what the two-substring match
   * actually ran against. Showing it pre-empts "you rigged the input". */
  if (last.raw_reply) {
    const quote = document.createElement("blockquote");
    quote.className = "compare__quote";
    quote.title = "Click to show the full reply";
    quote.addEventListener("click", () => quote.classList.toggle("is-open"));
    quote.textContent = last.raw_reply.trim().split("\n")[0];
    box.appendChild(quote);
  }

  host.appendChild(box);
}

function renderBlueprintTally(tally) {
  if (!tally) return;
  const total =
    tally.agreed + tally.unsafe + tally.spurious + tally.not_comparable;

  $("bp-count").textContent = total ? `over ${total} inspections` : "";

  const unsafe = $("bp-unsafe");
  unsafe.textContent = String(tally.unsafe);
  unsafe.dataset.tone = tally.unsafe > 0 ? "fail" : "";

  const spurious = $("bp-spurious");
  spurious.textContent = String(tally.spurious);
  spurious.dataset.tone = tally.spurious > 0 ? "hold" : "";

  $("bp-agreed").textContent = String(tally.agreed);
}

/* -------------------------------------------------------------------- chain */

/* A broken audit chain means the record of what this device decided can no
 * longer be trusted. That is not something an operator should have to run a
 * command to find out, so it sits in the masthead permanently. */
function renderChain(chain) {
  const pill = $("chain");
  if (!pill || !chain) return;
  pill.dataset.ok = String(chain.ok);
  pill.textContent =
    chain.status === "intact"
      ? `audit chain verified · ${chain.verified}`
      : chain.status === "empty"
        ? "audit chain empty"
        : chain.status === "truncated"
          ? "audit chain truncated"
          : "AUDIT CHAIN BROKEN";
  pill.title = chain.detail || "";
}

/* ---------------------------------------------------------------- checklist */

function renderChecklist(last) {
  const list = $("checklist");
  list.innerHTML = "";
  if (!manifest) return;

  const reported = new Map();
  if (last) for (const item of last.items) reported.set(item.key, item);

  let resolved = 0;

  for (const spec of manifest.items) {
    const seen = reported.get(spec.key);
    const presence = seen ? seen.presence : "unreported";
    /* Confirmed means this item is genuinely cleared. An expired or short item
     * is "found" and still a reason the kit failed, so counting it here would
     * put "7/7 confirmed" above a failing checklist. */
    if (
      seen &&
      presence === "found" &&
      !seen.unresolved &&
      !seen.expired &&
      !seen.short
    ) {
      resolved += 1;
    }

    const row = document.createElement("li");
    row.className = "item";
    row.dataset.presence = presence;
    row.dataset.blamed = seen ? String(seen.blamed) : "false";
    row.dataset.expired = seen ? String(Boolean(seen.expired)) : "false";
    row.dataset.short = seen ? String(Boolean(seen.short)) : "false";
    row.dataset.unresolved = seen ? String(seen.unresolved) : "false";

    const glyph = document.createElement("span");
    glyph.className = "item__glyph";
    glyph.textContent = seen && seen.expired
      ? "\u00d7"
      : seen && seen.short
        ? "\u2039"
        : GLYPH[presence] || "?";
    glyph.setAttribute("aria-hidden", "true");

    const label = document.createElement("span");
    label.className = "item__label";
    label.textContent = spec.label;
    if (spec.severity === "advisory" || spec.quantity > 1) {
      const note = document.createElement("small");
      const bits = [];
      /* Show counted-against-required rather than just the requirement. A row
       * reading "x2" beside a green tick looks compliant even when only one is
       * there, which is the whole failure this check exists to catch. */
      if (spec.quantity > 1) {
        const got = seen && seen.count !== null && seen.count !== undefined
          ? seen.count
          : "?";
        bits.push(`${got}/${spec.quantity}`);
      }
      if (spec.severity === "advisory") bits.push("advisory");
      note.textContent = bits.join(" \u00b7 ");
      if (seen && seen.short) note.dataset.state = "short";
      else if (spec.quantity > 1 && seen && seen.count == null) {
        note.dataset.state = "unknown";
      }
      label.appendChild(note);
    }

    const state = document.createElement("span");
    state.className = "item__presence";
    state.textContent = presence;

    const confidence = document.createElement("span");
    confidence.className = "item__confidence";
    if (seen && seen.confidence !== null) {
      confidence.textContent = seen.confidence.toFixed(2);
      confidence.dataset.belowFloor = String(
        seen.confidence < manifest.confidence_floor
      );
    } else {
      confidence.textContent = "—";
      confidence.dataset.none = "true";
    }

    /* An expired item is present and undamaged, so every other column on this
     * row says it is fine. The date is the only thing that disagrees, which is
     * exactly why it has to be legible. */
    const expiry = document.createElement("span");
    expiry.className = "item__expiry";
    if (spec.expiry_checked) {
      if (!seen || !seen.expiry) {
        expiry.textContent = "unreadable";
        expiry.dataset.state = "unreadable";
      } else {
        expiry.textContent = seen.expiry;
        expiry.dataset.state = seen.expired
          ? "expired"
          : seen.expiring_soon
            ? "soon"
            : "ok";
      }
    } else {
      expiry.textContent = "";
      expiry.dataset.state = "none";
    }

    row.append(glyph, label, state, confidence, expiry);
    if (seen && seen.note) row.title = seen.note;
    list.appendChild(row);
  }

  $("items-count").textContent = last
    ? `${resolved}/${manifest.items.length} confirmed`
    : `${manifest.items.length} required`;
}

/* ---------------------------------------------------------------- telemetry */

function renderTelemetry(telemetry) {
  if (!telemetry || !telemetry.available) {
    for (const id of ["t-latch", "t-indicator", "t-buzzer", "t-link", "t-seq"]) {
      $(id).textContent = "not simulated";
      $(id).dataset.tone = "";
    }
    return;
  }

  const latch = $("t-latch");
  latch.textContent = telemetry.latch;
  latch.dataset.tone = telemetry.latch === "released" ? "pass" : "";

  const indicator = $("t-indicator");
  indicator.textContent = telemetry.indicator;
  indicator.dataset.tone =
    { pass: "pass", fail: "fail", hold: "hold", stale: "stale" }[
      telemetry.indicator
    ] || "";

  const buzzer = $("t-buzzer");
  buzzer.textContent = telemetry.buzzer ? "sounding" : "silent";
  buzzer.dataset.tone = telemetry.buzzer ? "fail" : "";

  const link = $("t-link");
  link.textContent = telemetry.link_stale
    ? `stale (>${telemetry.link_timeout_ms}ms)`
    : "live";
  link.dataset.tone = telemetry.link_stale ? "stale" : "pass";

  $("t-seq").textContent =
    telemetry.last_seq === null ? "—" : String(telemetry.last_seq);

  const events = $("t-events");
  events.innerHTML = "";
  for (const line of [...telemetry.events].reverse()) {
    const row = document.createElement("div");
    row.textContent = line;
    events.appendChild(row);
  }
}

/* ------------------------------------------------------------------ records */

function renderRecords(rows, tally) {
  const body = $("records");
  const empty = $("records-empty");
  body.innerHTML = "";

  if (!rows.length) {
    empty.hidden = false;
    $("tally").innerHTML = "";
    return;
  }
  empty.hidden = true;

  const total = tally.pass + tally.fail + tally.indeterminate;
  $("tally").innerHTML =
    `<span>${total} total</span>` +
    `<span class="t-pass">pass <b>${tally.pass}</b></span>` +
    `<span class="t-fail">fail <b>${tally.fail}</b></span>` +
    `<span class="t-hold">indeterminate <b>${tally.indeterminate}</b></span>`;

  for (const row of rows) {
    const tr = document.createElement("tr");

    const time = document.createElement("td");
    time.className = "c-time";
    time.textContent = String(row.started_at || "").slice(11, 19);

    const verdict = document.createElement("td");
    const tag = document.createElement("span");
    tag.className = "tag";
    tag.dataset.verdict = row.verdict;
    tag.textContent = (VERDICT_WORD[row.verdict] || row.verdict).toLowerCase();
    verdict.appendChild(tag);

    const reason = document.createElement("td");
    reason.className = "c-reason";
    reason.textContent = row.reason;

    const commanded = document.createElement("td");
    commanded.className = "c-time";
    commanded.textContent = row.commanded;

    const id = document.createElement("td");
    id.className = "c-id";
    id.textContent = row.inspection_id;

    tr.append(time, verdict, reason, commanded, id);
    body.appendChild(tr);
  }
}

/* -------------------------------------------------------------------- wiring */

function describeScene() {
  const chosen = scenes.find((s) => s.name === $("scene").value);
  $("scene-description").textContent = chosen ? chosen.description : " ";
}

/* Each panel renders independently. A panel that throws must not take the
 * rest of the console with it - losing the latch readout because a tally
 * failed to draw would be the worst possible failure mode on this screen. */
function renderSafely(name, render) {
  try {
    render();
  } catch (error) {
    console.error(`[readykit] ${name} failed to render:`, error);
  }
}

/* The picture the verdict was decided on, with each item's reading beside it.
 * Reloaded only when a new inspection lands, keyed by its id, so the labels
 * can never sit under a different picture from the one they describe. */
let judgedId = null;

function renderJudged(last) {
  if (!source.live) return;
  const id = last ? last.inspection_id : null;
  if (id === judgedId) return;
  judgedId = id;

  const img = $("camera-judged");
  const tags = $("judged-tags");
  tags.innerHTML = "";
  if (!last) return;

  img.onload = () => { img.hidden = false; $("judged-empty").hidden = true; };
  img.onerror = () => {
    img.hidden = true;
    $("judged-empty").hidden = false;
    $("judged-empty").textContent = "No picture was captured for this inspection.";
  };
  img.src = `/api/inspected.jpg?id=${encodeURIComponent(id)}`;
  $("judged-when").textContent = last.started_at
    ? new Date(last.started_at).toLocaleTimeString()
    : "";

  for (const item of last.items) {
    const tag = document.createElement("li");
    tag.className = "seen__row";
    let state;
    if (item.model_said) {
      /* The model named this item, but said no kit is in view, so the
       * reading was set aside. Show its words, marked as not counted. */
      tag.dataset.presence = "set-aside";
      tag.title = "Not counted: the model said no kit is in view";
      state = item.model_said === "unreadable"
        ? "unreadable"
        : `${item.model_said} · not counted`;
    } else {
      tag.dataset.presence = item.presence;
      const confidence =
        typeof item.confidence === "number" && item.presence !== "unreported"
          ? ` ${Math.round(item.confidence * 100)}%`
          : "";
      state = `${item.presence}${confidence}`;
    }
    const dot = document.createElement("span");
    dot.className = "seen__dot";
    const name = document.createElement("span");
    name.className = "seen__name";
    name.textContent = item.label;
    const value = document.createElement("span");
    value.className = "seen__state";
    value.textContent = state;
    tag.append(dot, name, value);
    tags.appendChild(tag);
  }

  $("judged-status").textContent = last.kit_absent
    ? "The model sees no kit laid out, so nothing it saw is counted. " +
      "Put the items down together, in front of the camera."
    : "";
}

/* A look takes tens of seconds. A counting timer makes that visible, so a
 * slow model reads as working rather than frozen. */
let judgingTimer = null;

function showJudging(on) {
  const status = $("judged-status");
  clearInterval(judgingTimer);
  if (!on) return;
  const began = Date.now();
  const tick = () => {
    status.textContent =
      `Judging the frame captured when you pressed Inspect… ` +
      `${Math.round((Date.now() - began) / 1000)}s`;
  };
  tick();
  judgingTimer = setInterval(tick, 1000);
}

/* Live view: fetch the next frame only once the previous one has arrived, so
 * a slow camera or a busy host never piles up requests. */
function startLiveView() {
  const img = $("camera-live");
  const next = () => setTimeout(() => {
    img.src = `/api/camera.jpg?t=${Date.now()}`;
  }, 250);
  img.onload = next;
  img.onerror = () => setTimeout(next, 1500);
  next();
}

/* ------------------------------------------------------------ browser camera */

/* The browser opens the camera, not the host. Started on the first Inspect
 * and left running, so the next press is instant and you can aim between
 * looks. */
let stream = null;

async function ensureBrowserCamera() {
  if (stream && stream.active) return;
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    throw new Error("this browser cannot open a camera from this page");
  }
  stream = await navigator.mediaDevices.getUserMedia({
    video: { width: { ideal: 1280 }, height: { ideal: 720 } },
    audio: false,
  });
  const video = $("camera-video");
  video.srcObject = stream;
  video.hidden = false;
  $("live-empty").hidden = true;
  await video.play();
  /* The first frames are dark while exposure settles. */
  await new Promise((resolve) => setTimeout(resolve, 700));
}

/* One frame, at most 960px on its longest side - the model gains nothing
 * from more, and it halves the time the model spends reading it. */
async function captureBrowserFrame() {
  const video = $("camera-video");
  const scale = Math.min(1, 960 / Math.max(video.videoWidth, video.videoHeight));
  const canvas = document.createElement("canvas");
  canvas.width = Math.round(video.videoWidth * scale);
  canvas.height = Math.round(video.videoHeight * scale);
  canvas.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);
  return new Promise((resolve, reject) =>
    canvas.toBlob(
      (blob) => (blob ? resolve(blob) : reject(new Error("could not capture a frame"))),
      "image/jpeg",
      0.9,
    ),
  );
}

async function refresh() {
  const [state, records] = await Promise.all([
    getJSON("/api/state"),
    getJSON("/api/records?limit=25"),
  ]);
  renderSafely("verdict", () => renderVerdict(state.last, state.telemetry));
  renderSafely("telemetry", () => renderTelemetry(state.telemetry));
  renderSafely("checklist", () => renderChecklist(state.last));
  renderSafely("judged", () => renderJudged(state.last));
  renderSafely("blueprint", () => renderBlueprintTally(state.blueprint_tally));
  renderSafely("chain", () => renderChain(state.chain));
  renderSafely("records", () => renderRecords(records.records, state.tally));
}

async function runInspection() {
  if (busy) return;
  busy = true;
  const button = $("run");
  button.disabled = true;
  button.textContent = "Inspecting…";
  if (source.live) showJudging(true);

  let failed = false;
  try {
    /* A live console sends no scene, and the server refuses one. The input
     * is whatever the camera is pointed at; there is nothing to choose. */
    if (source.browser_camera) {
      await ensureBrowserCamera();
      await getJSON("/api/inspect-frame", {
        method: "POST",
        headers: { "Content-Type": "image/jpeg" },
        body: await captureBrowserFrame(),
      });
    } else {
      await getJSON("/api/inspect", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: source.live ? "{}" : JSON.stringify({ scene: $("scene").value }),
      });
    }
    await refresh();
  } catch (error) {
    const denied = error && error.name === "NotAllowedError";
    const message = denied
      ? "Camera access was blocked. Allow the camera for this page in the browser's address bar, then press Inspect again."
      : `Console error: ${error.message}`;
    $("verdict-reason").textContent = message;
    if (source.live) $("judged-status").textContent = message;
    $("auto").checked = false;
    failed = true;
  } finally {
    if (source.live) {
      showJudging(false);
      /* Redraw for the verdict just landed - but not after a failure, or the
       * next poll would wipe the message saying what went wrong. */
      if (!failed) judgedId = null;
    }
    busy = false;
    button.disabled = false;
    button.textContent = "Inspect";
  }
  /* Auto: start the next look as soon as this one is decided. No fixed
   * timer - the model's own pace sets the rhythm, so looks never overlap. */
  if (source.live && $("auto").checked) setTimeout(runInspection, 500);
}

async function boot() {
  manifest = await getJSON("/api/manifest");
  $("manifest-name").textContent = `· ${manifest.name}`;

  source = await getJSON("/api/source");

  if (source.live) {
    /* No picker in front of a real camera. An operator who reads a dropdown
     * of scene names believes the input is scripted, and a verdict about
     * their actual kit would be shown under the name of a rehearsal. */
    $("scene").hidden = true;
    $("source-live").hidden = false;
    $("source-live").title = "This console is inspecting real frames";
    $("source-input").textContent = source.source;
    $("source-engine").textContent = source.engine;
    $("scene-description").textContent =
      "Inspecting real frames. Point the camera at the kit and press Inspect.";
    $("camera").hidden = false;
    $("auto-wrap").hidden = false;
    /* Put Inspect beside the camera it acts on, instead of a scroll away. */
    const actions = $("camera-actions");
    for (const id of ["source-live", "run", "auto-wrap"]) actions.appendChild($(id));
    for (const id of ["run-title", "run-controls", "scene-description"]) $(id).hidden = true;
    $("auto").addEventListener("change", () => {
      if ($("auto").checked) runInspection();
    });
    if (source.browser_camera) {
      $("camera-live").hidden = true;
      $("live-empty").hidden = false;
    } else {
      startLiveView();
    }
  } else {
    const payload = await getJSON("/api/scenes");
    scenes = payload.scenes;

    const select = $("scene");
    for (const scene of scenes) {
      const option = document.createElement("option");
      option.value = scene.name;
      option.textContent = scene.name;
      select.appendChild(option);
    }
    select.addEventListener("change", describeScene);
    describeScene();
  }

  $("run").addEventListener("click", runInspection);

  await refresh();
  /* Poll for latch and watchdog changes - the hold expires and the link goes
   * stale on the node's own clock, with no request from us. */
  setInterval(() => refresh().catch(() => {}), 1000);
}

boot().catch((error) => {
  $("verdict-reason").textContent = `Could not reach the host: ${error.message}`;
});
