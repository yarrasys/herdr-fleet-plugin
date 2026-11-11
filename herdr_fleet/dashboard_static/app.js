/* Signal Box: live observations and deliberate owner actions. No terminal input path. */
"use strict";
(function () {
  let capability = location.hash.slice(1);
  history.replaceState(null, "", location.pathname);
  const $ = function (id) { return document.getElementById(id); };
  const state = {csrf: null, data: null, fleet: null, seat: null, targetFleet: null, tab: "terminal",
    question: null, reply: null, drafts: new Map(), intent: null, watch: null, streamKey: null,
    first: true, seen: new Set(), motion: !matchMedia("(prefers-reduced-motion: reduce)").matches,
    expanded: false, collapsed: false, messageMailbox: "owner", tick: null,
    motionChosen: false, unseen: new Map(), ownerSeen: new Set(), tally: null, flights: 0, authEpoch: 0,
    threads: new Map(), editGeneration: 0, replyGeneration: 0, readGeneration: 0,
    conversationRequest: 0, historyRequest: 0, quiet: false, savedReplies: new Map(), threadAccess: 0,
    attention: null, attentionScope: "all", attentionCategory: "all", attentionRequest: 0, detailRequest: 0,
    preferenceMode: null, preferenceWarning: false, terminalHeight: 360};
  const attentionTypes = ["owner_message", "native_dialog", "send_issue", "provider_hold", "observation_issue"];
  const attentionView = typeof AttentionView !== "undefined" ? AttentionView.mount($("attention-desk"), {
    onAction: function (id) { actAttention(id).catch(fail); },
    onFilter: function (scope, category) {
      if (!["all", "selected"].includes(scope) || !["all"].concat(attentionTypes).includes(category)) return;
      state.attentionScope = scope; state.attentionCategory = category;
      state.attention = null; renderAttention();
      saveView();
      refreshAttention().catch(fail);
    }
  }) : null;
  function preferenceKey() { return state.preferenceMode ? "herdr-fleet.view.v1." + state.preferenceMode : null; }
  function defaultView() {
    return {version: 1, collapsed: false, motion: null, height: 360, scope: "all", category: "all"};
  }
  function validView(value) {
    return value && typeof value === "object" && !Array.isArray(value) &&
      Object.keys(value).sort().join(",") === "category,collapsed,height,motion,scope,version" && value.version === 1 &&
      typeof value.collapsed === "boolean" && (value.motion === null || typeof value.motion === "boolean") &&
      Number.isInteger(value.height) && value.height >= 200 && value.height <= 900 &&
      ["all", "selected"].includes(value.scope) && ["all"].concat(attentionTypes).includes(value.category);
  }
  function applyView(value) {
    state.collapsed = value.collapsed; state.motionChosen = value.motion !== null;
    state.motion = state.motionChosen ? value.motion : !matchMedia("(prefers-reduced-motion: reduce)").matches;
    state.attentionScope = value.scope; state.attentionCategory = value.category;
    $("app").classList.toggle("collapsed", state.collapsed);
    $("collapse").textContent = state.collapsed ? "Expand fleets" : "Collapse fleets";
    $("collapse").setAttribute("aria-expanded", String(!state.collapsed));
    height(value.height); motion();
  }
  function preferenceStatus() {
    $("view-pref-status").textContent = state.preferenceWarning ?
      "View settings are temporary; browser storage is unavailable or invalid." :
      "Only cosmetic settings are stored for this origin. A different port has separate settings. Drafts are not saved.";
  }
  function loadView(demo) {
    const mode = demo ? "demo" : "live";
    if (state.preferenceMode === mode) return;
    state.preferenceMode = mode; state.preferenceWarning = false;
    state.attention = null; state.attentionRequest++;
    let value = defaultView();
    try {
      const raw = localStorage.getItem(preferenceKey());
      if (raw !== null) {
        if (typeof raw !== "string" || raw.length > 2048 || new TextEncoder().encode(raw).length > 2048) throw new Error("size");
        const parsed = JSON.parse(raw); if (!validView(parsed)) throw new Error("schema"); value = parsed;
      }
    } catch (error) { state.preferenceWarning = true; }
    applyView(value); preferenceStatus();
  }
  function saveView() {
    if (!preferenceKey()) return;
    const value = {version: 1, collapsed: state.collapsed, motion: state.motionChosen ? state.motion : null,
      height: state.terminalHeight, scope: state.attentionScope, category: state.attentionCategory};
    try {
      const raw = JSON.stringify(value);
      if (!validView(value) || new TextEncoder().encode(raw).length > 2048) throw new Error("schema");
      localStorage.setItem(preferenceKey(), raw); state.preferenceWarning = false;
    } catch (error) { state.preferenceWarning = true; }
    preferenceStatus();
  }
  const conversationView = typeof ConversationView !== "undefined" ? ConversationView.mount($("conversation"), {
    onOlder: function () { const item = thread(); item.limit = Math.min(100, item.limit + 50); refreshConversation(); },
    onSeen: function (key) { const item = state.threads.get(key); if (item) item.unseen = 0; },
    onReply: function (key, id) {
      if (key !== threadKey()) return;
      chooseReply(id); render(); $("draft").focus();
    }
  }) : null;
  const recentConversations = conversationView ? document.createElement("nav") : null;
  if (recentConversations) {
    recentConversations.className = "toolbar";
    recentConversations.setAttribute("aria-label", "Recent conversations");
    $("conversation-panel").insertBefore(recentConversations, $("conversation"));
  }
  const term = new Terminal({disableStdin: true, cursorBlink: false, convertEol: false, scrollback: 1000,
    fontFamily: 'ui-monospace, "SF Mono", Menlo, Consolas, monospace', fontSize: 12,
    theme: {background: "#02050a", foreground: "#dcebf4", cursor: "#5fd8ff"},
    allowProposedApi: false, rightClickSelectsWord: true,
    windowOptions: {}, linkHandler: {activate: function () {}},
    clipboardProvider: {readText: function () { return Promise.resolve(""); }, writeText: function () { return Promise.resolve(); }}});
  const fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  term.open($("terminal-screen"));
  const helper = $("terminal-screen").querySelector(".xterm-helper-textarea");
  if (helper) { helper.readOnly = true; helper.tabIndex = -1; helper.setAttribute("aria-hidden", "true"); }
  function node(tag, text, className) {
    const item = document.createElement(tag);
    if (text !== undefined && text !== null) item.textContent = String(text);
    if (className) item.className = className;
    return item;
  }
  function notice(text) { $("notice").textContent = text; }
  function status(text) {
    const demo = state.data && state.data.demo;
    $("terminal-status").textContent = (demo && !/^DEMO/.test(text) ? "DEMO · " : "") + text;
  }
  function fail(error) { notice(error.message || "Connection unavailable. Reopen the local launch address."); }
  async function api(path, body, signal) {
    const headers = {"X-Fleet-CSRF": state.csrf};
    const options = {headers: headers, cache: "no-store", credentials: "same-origin", signal: signal};
    if (body !== undefined) { options.method = "POST"; headers["Content-Type"] = "application/json"; options.body = JSON.stringify(body); }
    const response = await fetch(path, options);
    if (!response.ok) {
      const data = await response.json().catch(function () { return {}; });
      throw new Error(data.error || "Request failed. Refresh before a deliberate new attempt.");
    }
    return response;
  }
  function currentFleet() { return state.data && state.data.fleets.find(function (f) { return f.id === state.fleet; }); }
  function targetFleet() { return state.data && state.data.fleets.find(function (f) { return f.id === state.targetFleet; }); }
  function currentSeat() { const f = targetFleet(); return f && f.seats.find(function (s) { return s.name === state.seat; }); }
  function draftKey() { return [state.fleet, state.targetFleet, state.seat, state.reply || ""].join("|"); }
  function saveDraft(event) {
    if (!state.fleet) return;
    const key = draftKey(), old = state.drafts.get(key), text = $("draft").value;
    const changed = !old || old.text !== text || (event && event.type === "input");
    const read = !!$("read-with-reply").checked;
    const readGeneration = old && old.read !== read ? ++state.readGeneration : old ? old.readGeneration || 0 : 0;
    state.drafts.set(key, {text: text, intent: state.intent, read: read, readGeneration: readGeneration,
      generation: changed ? ++state.editGeneration : old.generation});
  }
  function restoreDraft() {
    const draft = state.drafts.get(draftKey());
    $("draft").value = draft ? draft.text : ""; state.intent = draft ? draft.intent : null;
    $("read-with-reply").checked = !!(draft && draft.read);
    $("new-attempt").hidden = !state.intent;
    $("action-result").textContent = state.intent && state.intent.description ? state.intent.description : "A confirmed delivery is not acceptance of work.";
  }
  function selectedChanged(fleetId, name) {
    const moved = fleetId !== state.targetFleet || name !== state.seat;
    saveDraft();
    if (moved) { disconnect("Watch disconnected: selected target changed. Screen cleared."); term.reset(); }
    state.targetFleet = fleetId; state.seat = name; state.reply = null; state.replyGeneration++;
    thread().unseen = 0;
    restoreDraft(); render();
  }
  function threadKey() { return [state.fleet, state.targetFleet, state.seat].join("|"); }
  function thread(key) {
    key = key || threadKey();
    if (!state.threads.has(key)) state.threads.set(key, {messages: [], local: new Map(), supersedes: new Map(), state: "loading", limit: 50, unseen: 0});
    const item = state.threads.get(key);
    if (key === threadKey()) item.visited = true;
    return item;
  }
  function chooseReply(id) {
    saveDraft(); state.reply = id; state.replyGeneration++; restoreDraft();
  }
  function savedReply() {
    const saved = state.savedReplies.get(threadKey());
    if (!saved) return null;
    for (const id of saved) {
      const draft = state.drafts.get(threadKey() + "|" + id);
      if (draft && (draft.text || draft.intent)) return id;
      saved.delete(id);
    }
    return null;
  }
  function selectConversation(key) {
    const identity = key.split("|"), source = state.data.fleets.find(function (f) { return f.id === identity[0]; });
    const target = state.data.fleets.find(function (f) { return f.id === identity[1]; });
    if (!source || !target || !target.seats.some(function (s) { return s.name === identity[2]; })) {
      notice("This conversation is no longer registered. Refresh before acting."); return;
    }
    saveDraft(); disconnect("Watch disconnected: conversation changed. Screen cleared."); term.reset();
    state.fleet = source.id; state.targetFleet = target.id; state.seat = identity[2];
    thread().unseen = 0;
    state.reply = null; state.replyGeneration++; state.question = null; $("owner-detail").hidden = true;
    restoreDraft(); render(); refreshConversation();
    if (state.tab !== "terminal") refreshTab().catch(fail);
  }
  function renderRecent() {
    if (!recentConversations || !state.data) return;
    const focused = document.activeElement && document.activeElement.dataset.focusKey;
    const recent = Array.from(state.threads).filter(function (pair) {
      const identity = pair[0].split("|");
      return pair[1].visited && identity[0] === state.fleet && identity[1] !== identity[0] &&
        state.data.fleets.some(function (f) { return f.id === identity[1] && f.seats.some(function (s) { return s.name === identity[2]; }); });
    }).sort(function (a, b) { return (b[1].access || 0) - (a[1].access || 0); }).slice(0, 8);
    const controls = recent.map(function (pair) {
      const identity = pair[0].split("|"), source = currentFleet();
      const target = state.data.fleets.find(function (f) { return f.id === identity[1]; });
      const label = String(source.label).slice(0, 80) + " → " + String(target.label).slice(0, 80) + " · " + identity[2];
      const control = button(label + (pair[1].unseen ? " · Activity +" + pair[1].unseen : ""), function () { selectConversation(pair[0]); });
      control.dataset.focusKey = "recent:" + pair[0];
      control.setAttribute("aria-pressed", String(pair[0] === threadKey()));
      return control;
    });
    recentConversations.hidden = !controls.length;
    recentConversations.replaceChildren.apply(recentConversations, controls);
    const same = controls.find(function (control) { return control.dataset.focusKey === focused; });
    if (same) same.focus({preventScroll: true});
  }
  function pruneCompletions(item) {
    const visible = new Map(item.messages.map(function (row) { return [row.id, row]; }));
    item.supersedes.forEach(function (_, id) { if (!visible.has(id)) item.supersedes.delete(id); });
    item.local.forEach(function (record, intent) {
      if (!record.retired || record.demo) return;
      const actual = visible.get((record.result || {}).id);
      if (actual && actual.from === "owner" && actual.to === record.seat) {
        record.reconciled = true;
        item.supersedes.set(actual.id, "intent:" + intent);
        if (actual.body !== null) item.local.delete(intent);
      } else if (record.reconciled) item.local.delete(intent);
    });
    const retained = Array.from(item.local).filter(function (pair) { return pair[1].retired && !pair[1].demo; });
    retained.slice(0, Math.max(0, retained.length - 100)).forEach(function (pair) { item.local.delete(pair[0]); });
    // Bodies of completed inactive conversations are a cache. Drafts and all
    // pending/uncertain attempts remain independently retained.
    const inactive = Array.from(state.threads).filter(function (pair) { return pair[0] !== threadKey(); })
      .sort(function (a, b) { return (b[1].access || 0) - (a[1].access || 0); });
    inactive.slice(31).forEach(function (pair) {
      pair[1].messages = []; pair[1].supersedes.clear();
      pair[1].local.forEach(function (record, id) { if (record.retired && !record.demo) pair[1].local.delete(id); });
    });
  }
  function renderConversation() {
    if (!conversationView || !state.data || !state.seat) return;
    const item = thread(), rows = item.messages.map(function (row) {
      const result = Object.assign({key: row.id}, row);
      if (result.incomplete) result.certainty = "unconfirmed";
      if (item.supersedes.has(row.id)) result.supersedes = item.supersedes.get(row.id);
      return result;
    });
    const indexed = new Map(rows.map(function (row) { return [row.id, row]; }));
    item.local.forEach(function (record, intent) {
      const result = record.result || {};
      let stored = indexed.get(result.id);
      if (!stored && record.demo) stored = rows.find(function (row) { return row.status === "local" && row.from === "owner" && row.to === record.seat && row.reply_to === record.reply; });
      const localKey = "intent:" + intent;
      if (stored) { stored.supersedes = localKey; if (stored.body === null || record.demo) stored.body = record.text; return; }
      rows.push({key: localKey, id: result.id || null, from: "owner", to: record.seat, time: record.time,
        body: record.text, status: record.pending ? "sending" : result.status || "unconfirmed",
        certainty: result.certainty || "unconfirmed", reply_to: record.reply, receipts: record.receipts || [],
        detail: result.message || "", incomplete: !record.pending && result.certainty === "unconfirmed"});
    });
    const ordered = rows.map(function (row, index) {
      const stamp = Date.parse(row.time);
      if (!Number.isFinite(stamp)) { row.incomplete = true; row.certainty = "unconfirmed"; }
      return {row: row, stamp: Number.isFinite(stamp) ? stamp : Infinity, index: index};
    }).sort(function (a, b) { return a.stamp === b.stamp ? a.index - b.index : a.stamp - b.stamp; });
    conversationView.render({thread: threadKey(), peer: state.seat, demo: !!state.data.demo,
      state: item.state, note: item.note || "", older: item.truncated ? "truncated" : "none",
      messages: ordered.map(function (item) { return item.row; }), quiet: state.quiet});
    renderRecent();
  }
  function retire(record, result) {
    if (result.certainty !== "confirmed" || !["queued", "delivered"].includes(result.status) || record.retired) return;
    record.retired = true;
    const draft = state.drafts.get(record.draftKey);
    if (!draft || draft.intent !== record.intent) return;
    const newerEdit = draft.generation !== record.generation;
    const description = record.seat + " · " + result.status + (result.id ? " · #" + result.id.slice(-6) : "") +
      " · " + result.certainty + " · delivery is not work acceptance";
    record.intent.description = description;
    draft.intent = null;
    if (draft.generation === record.generation) draft.text = "";
    const retireRead = record.reply && draft.readGeneration === record.readGeneration;
    if (retireRead) draft.read = false;
    if (record.reply) {
      if (!state.savedReplies.has(record.threadKey)) state.savedReplies.set(record.threadKey, new Set());
      const saved = state.savedReplies.get(record.threadKey);
      if (draft.text) saved.add(record.reply); else saved.delete(record.reply);
    }
    if (draftKey() === record.draftKey && state.intent === record.intent) {
      let restoredIndependent = false;
      state.intent = null;
      if ($("draft").value !== draft.text) $("draft").value = draft.text;
      if (retireRead) $("read-with-reply").checked = false;
      if (record.reply && state.reply === record.reply && state.replyGeneration === record.replyGeneration) {
        state.reply = null; state.replyGeneration++;
        const plain = state.drafts.get(draftKey());
        if (plain && (plain.text || plain.intent)) {
          restoreDraft();
          restoredIndependent = true;
          if (draft.text) notice("Reply delivered. Your independent plain draft is restored; Continue saved reply draft keeps your newer edit available.");
        } else {
          saveDraft();
          if (draft.text) {
            draft.text = "";
            state.savedReplies.get(record.threadKey).delete(record.reply);
          }
        }
      }
      if (!restoredIndependent) $("action-result").textContent = (newerEdit ? "Previous message · " : "") + description +
        (newerEdit ? ". Newer draft retained." : "");
      $("new-attempt").hidden = !state.intent;
    }
  }
  async function refreshConversation() {
    if (!state.csrf || !state.seat || document.hidden) return;
    const key = threadKey(), epoch = state.authEpoch, request = ++state.conversationRequest, item = thread(key);
    const query = "?source=" + encodeURIComponent(state.fleet) + "&fleet=" + encodeURIComponent(state.targetFleet) +
      "&seat=" + encodeURIComponent(state.seat) + "&limit=" + item.limit;
    try {
      const data = await (await api("/api/conversation" + query)).json();
      if (epoch !== state.authEpoch || request !== state.conversationRequest || key !== threadKey()) return;
      item.messages = data.messages || []; item.truncated = data.truncated;
      item.access = ++state.threadAccess;
      item.state = item.messages.some(function (row) { return row.incomplete; }) ? "stale" : "ready";
      item.note = data.incomplete ? "Limited history · some prior records or receipts are incomplete." : "";
      (data.intents || []).forEach(function (association) {
        const record = item.local.get(association.intent);
        const actual = item.messages.find(function (row) { return row.id === association.id && row.from === "owner" && row.to === record?.seat; });
        if (!record || !actual) return;
        record.result = {id: actual.id, status: actual.status, certainty: actual.incomplete ? "unconfirmed" : actual.certainty};
        record.pending = false;
        retire(record, record.result);
      });
      pruneCompletions(item);
      renderConversation();
    } catch (error) {
      if (epoch !== state.authEpoch || request !== state.conversationRequest || key !== threadKey()) return;
      item.state = "stale"; item.note = "Disconnected · last conversation retained. " + error.message;
      renderConversation();
    }
  }
  function lamp(status) {
    const item = node("span", "", "lamp " + (["working", "blocked", "idle", "done", "absent", "question"].includes(status) ? status : "unknown"));
    item.setAttribute("aria-label", status); return item;
  }
  function fleetCode(label) {
    const words = String(label).trim().split(/\s+/).filter(Boolean);
    return (words.length > 1 ? words.map(function (w) { return w[0]; }).join("").slice(0, 3) : String(label).slice(0, 3)).toUpperCase();
  }
  function questionsOf(fleet) { return fleet.owner.filter(function (m) { return !m.fyi; }); }
  function renderBuild() {
    const build = state.data && state.data.build;
    if (!build) return;
    const version = function (value) { return value && typeof value.version === "string" ? value.version.slice(0, 64) : "unknown"; };
    const commit = function (value) { return value && typeof value.commit === "string" && /^[a-f0-9]{40,64}$/.test(value.commit) ? value.commit.slice(0, 8) : "commit unknown"; };
    $("build-running").textContent = "Running " + version(build.running) + " · " + commit(build.running);
    $("build-installed").textContent = "Installed " + version(build.installed) + " · " + commit(build.installed);
    $("build-status").textContent = build.comparison === "different" ? "Running a different build" :
      build.comparison === "current" ? "Running the installed build" : "Installed build comparison unknown";
    $("build-status").classList.toggle("upd", build.comparison === "different");
    $("build-guidance").textContent = build.comparison === "different" ?
      "Stop the foreground dashboard with Ctrl-C, " + (build.restart === "all_fleets" ?
      "run herdr-fleet dashboard --all-fleets --open" : "rerun your original dashboard command with --open") +
      ", and use the fresh tab. Page Refresh does not replace the running package; preserve drafts before restarting." :
      "Installed identity is checked at most once per 60 seconds. No automatic update or restart.";
  }
  function renderAttention() {
    if (!attentionView || !state.data) return;
    attentionView.render(Object.assign({}, state.attention || {items: [], counts: {}, incomplete: true}, {
      scope: state.attentionScope, category: state.attentionCategory, quiet: state.quiet || state.first,
      fleets: state.data.fleets.map(function (fleet) { return {id: fleet.id, label: fleet.label}; })}));
  }
  async function refreshAttention() {
    if (!attentionView || !state.csrf || !state.data || document.hidden) return;
    const epoch = state.authEpoch, request = ++state.attentionRequest;
    const scope = state.attentionScope, category = state.attentionCategory, fleet = state.fleet;
    let query = "?scope=" + encodeURIComponent(scope) + "&category=" + encodeURIComponent(category);
    if (scope === "selected" && fleet) query += "&fleet=" + encodeURIComponent(fleet);
    try {
      const data = await (await api("/api/attention" + query)).json();
      if (epoch !== state.authEpoch || request !== state.attentionRequest || scope !== state.attentionScope ||
          category !== state.attentionCategory || (scope === "selected" && fleet !== state.fleet)) return;
      state.attention = data; renderAttention();
    } catch (error) {
      if (epoch !== state.authEpoch || request !== state.attentionRequest) return;
      state.attention = Object.assign({}, state.attention || {items: [], counts: {}}, {stale: true, incomplete: true});
      renderAttention();
    }
  }
  async function actAttention(id) {
    const item = state.attention && state.attention.items.find(function (row) { return row.id === id; });
    if (!item || !item.actions || item.actions.length !== 1 ||
        (state.attention.stale && item.actions[0].kind !== "refresh")) {
      notice("This attention item is no longer actionable; refresh the observation."); return;
    }
    const action = item.actions[0], fleets = state.data.fleets;
    const source = fleets.find(function (fleet) { return fleet.id === (action.source || action.fleet || item.fleet); });
    if (action.kind === "refresh" && source) { await refresh(); return; }
    if (!source || source.stale) { notice("The source observation is unavailable; refresh before acting."); return; }
    if (action.kind === "inspect_owner") {
      selectFleet(source.id); await inspectQuestion({id: action.message}); return;
    }
    if (action.kind === "inspect_log") { selectFleet(source.id); tab("log"); return; }
    const fleet = fleets.find(function (f) { return f.id === action.fleet; });
    const seat = fleet && fleet.seats.find(function (s) { return s.name === action.seat; });
    if (!fleet || fleet.stale || !seat) { notice("The registered seat changed; refresh before acting."); return; }
    if (action.kind === "watch" && (!seat.view || action.view !== seat.view || seat.state !== "blocked")) {
      notice("The native dialog or its seat binding changed; refresh before Watch."); return;
    }
    if (!["watch", "inspect_send", "inspect_seat"].includes(action.kind)) return;
    if (action.kind === "watch") tab("terminal");
    selectFleet(source.id); selectedChanged(fleet.id, seat.name);
    if (action.kind === "watch") await watch();
    else if (action.kind === "inspect_seat") notice("Recorded provider hold. Inspect it here; no release, failover or interruption is performed.");
  }
  function selectFleet(id) {
    if (state.fleet === id) return;
    const fleet = state.data.fleets.find(function (f) { return f.id === id; }); if (!fleet) return;
    saveDraft(); disconnect("Watch disconnected: fleet changed. Screen cleared."); term.reset(); $("travel").replaceChildren();
    state.fleet = fleet.id; state.targetFleet = fleet.id; state.seat = fleet.seats[0] ? fleet.seats[0].name : null;
    if (state.attentionScope === "selected") { state.attention = null; state.attentionRequest++; }
    if (state.seat) thread().unseen = 0;
    state.question = null; state.reply = null; state.replyGeneration++; $("owner-detail").hidden = true;
    restoreDraft(); render(); refreshTab().catch(fail);
  }
  /* Where a name sits along the shown fleet's line: a seat's lamp, the owner at the column edge, anyone else at the far end. */
  function anchor(name) {
    if (name === "owner") return 0;
    const seat = Array.from($("seats").children).find(function (b) { return b.dataset.seat === name; });
    const wrap = $("travel").getBoundingClientRect();
    if (!seat || !wrap.width) return 1;
    const lampBox = seat.querySelector(".lamp").getBoundingClientRect();
    return Math.max(0, Math.min(1, (lampBox.left + lampBox.width / 2 - wrap.left) / wrap.width));
  }
  function ring(name, kind) {
    if (!state.motion) return;
    const item = node("span", "", "ring " + kind); item.style.left = anchor(name) * 100 + "%";
    $("travel").append(item);
    const run = item.animate([{transform: "scale(1)", opacity: .95}, {transform: "scale(5)", opacity: 0}], {duration: 900, easing: "ease-out"});
    const done = function () { item.remove(); }; run.finished.then(done, done);
  }
  /* A logged send crosses the line carrying its own id. Cosmetic only: the receipt text is already on screen. */
  function travel(event, landed) {
    if (!state.motion || state.flights >= 6) { if (landed) ring(event.to, landed); return; }
    const capsule = node("span", "", "capsule" + (event.from === "owner" || event.to === "owner" ? " owner" : ""));
    capsule.append(node("i"), node("b", "#" + event.id.slice(-6)));
    const from = anchor(event.from) * 100 + "%", to = anchor(event.to) * 100 + "%";
    capsule.style.left = to; $("travel").append(capsule); state.flights++;
    const run = capsule.animate([{left: from}, {left: to}], {duration: 1300, easing: "cubic-bezier(.45,.05,.3,1)"});
    const done = function () { state.flights = Math.max(0, state.flights - 1); capsule.remove(); if (landed) ring(event.to, landed); };
    run.finished.then(done, done);
  }
  function bump(item) {
    if (state.motion && item) item.animate([{transform: "scale(1)"}, {transform: "scale(1.28)", filter: "brightness(1.7)"}, {transform: "scale(1)"}], {duration: 700, easing: "ease-out"});
  }
  function logBadge() {
    const count = state.unseen.get(state.fleet) || 0;
    $("log-count").hidden = !count || state.tab === "log"; $("log-count").textContent = String(count);
  }
  function button(text, action) { const b = node("button", text); b.type = "button"; b.addEventListener("click", action); return b; }
  function ago(value) {
    const stamp = Date.parse(value); if (!Number.isFinite(stamp)) return "time unknown";
    const seconds = Math.max(0, Math.floor((Date.now() - stamp) / 1000));
    return seconds < 60 ? seconds + "s ago" : Math.floor(seconds / 60) + "m ago";
  }
  function render() {
    const focusedKey = document.activeElement && document.activeElement.dataset.focusKey;
    const data = state.data; if (!data) return;
    loadView(!!data.demo);
    renderBuild();
    const fleets = data.fleets;
    $("mode").textContent = data.demo ? "DEMO · Authored synthetic run · No live actions" : "LIVE · Authenticated local owner";
    $("persistent-mode").textContent = $("mode").textContent;
    $("replay").hidden = !data.demo;
    const allAttention = data.attention;
    const tally = allAttention ? attentionTypes.reduce(function (n, type) { return n + (allAttention.counts[type] || 0); }, 0) :
      fleets.reduce(function (n, f) { return n + questionsOf(f).length; }, 0);
    $("attention-count").textContent = String(tally);
    if (state.tally !== null && state.tally !== tally) bump($("attention-count"));
    state.tally = tally;
    const navigation = fleets.map(function (fleet, index) {
      const categories = allAttention && allAttention.fleet_counts && allAttention.fleet_counts[fleet.id];
      const asks = categories ? attentionTypes.reduce(function (n, type) { return n + (categories[type] || 0); }, 0) : questionsOf(fleet).length;
      const fresh = state.unseen.get(fleet.id) || 0;
      const b = button("", function () { selectFleet(fleet.id); });
      b.className = "fleet-button" + (fleet.stale ? " stale" : "") + (asks ? " hold" : "");
      b.dataset.focusKey = "fleet:" + fleet.id;
      b.setAttribute("aria-current", String(state.fleet === fleet.id));
      b.setAttribute("aria-label", fleet.label + " · " + (fleet.stale ? "stale" : "observed " + ago(fleet.observed_at)) +
        " · " + asks + " observed attention items · " + fleet.seats.map(function (s) { return s.name + " " + s.state; }).join(", "));
      b.title = fleet.label;
      const top = node("span", "", "fleet-top");
      top.append(node("span", String(index + 1).padStart(2, "0"), "fleet-idx"), node("strong", fleet.label, "fleet-name"), node("span", fleetCode(fleet.label), "fleet-code"));
      const signals = node("span", "", "fleet-signals");
      fleet.seats.slice(0, 7).forEach(function (s) { signals.append(lamp(s.state)); });
      if (asks) signals.append(lamp("question"));
      const meta = node("span", (fleet.stale ? "Stale · " : "") + ago(fleet.observed_at) + " · " + asks + " observed attention", "fleet-meta" + (asks ? " hot" : ""));
      b.append(top, signals, meta);
      if (categories) {
        const marks = node("span", "", "fleet-kinds"), tones = ["question", "dialog", "send", "limit", "stale"];
        attentionTypes.forEach(function (type, i) {
          if (!categories[type]) return;
          const mark = node("span"); mark.append(node("i", "", "mark " + tones[i]), node("b", String(categories[type])));
          mark.setAttribute("aria-label", type.replace(/_/g, " ") + ": " + categories[type]); marks.append(mark);
        });
        b.append(marks);
      }
      if (fresh && state.fleet !== fleet.id) b.append(node("span", fresh + " new", "fleet-new"));
      return b;
    });
    $("fleets").replaceChildren.apply($("fleets"), navigation);
    const fleet = currentFleet();
    if (!fleet) { $("fleet-title").textContent = "No registered fleets"; document.title = (data.demo ? "DEMO" : "LIVE") + " · Signal Box"; return; }
    $("fleet-title").textContent = fleet.label;
    document.title = (data.demo ? "DEMO · " : "LIVE · ") + fleet.label + " · Signal Box";
    $("freshness").textContent = (fleet.stale ? "STALE · " : "Observed ") + (fleet.observed_at ? new Date(fleet.observed_at).toLocaleTimeString() + " · " + ago(fleet.observed_at) : "unknown");
    $("watcher").textContent = fleet.watcher && fleet.watcher.enabled === false ? "Watcher intentionally disabled by fleet configuration" :
      fleet.watcher && fleet.watcher.running === true ? "Watcher process running" :
      fleet.watcher && fleet.watcher.running === false ? "Watcher stopped · no automatic startup" : "Watcher liveness unknown · no automatic startup";
    if (fleet.warning) notice(fleet.warning);
    const questions = fleet.owner.map(function (message) {
      const row = node("div", "", "question-row" + (message.fyi ? " report" : "")); const label = node("span");
      const words = node("span", message.sender + (message.fyi ? " reports" : " asks you"));
      words.append(node("small", "#" + message.id.slice(-6) + " · " + ago(message.time) + (message.fyi ? " · FYI: the seat's own report, not a checked result" : " · plain message, no preset choices")));
      label.append(lamp(message.fyi ? "idle" : "question"), words);
      const inspect = button(message.fyi ? "Inspect report" : "Inspect question", function () { inspectQuestion(message).catch(fail); });
      if (!message.fyi) inspect.className = "attention";
      inspect.dataset.focusKey = "question:" + message.id;
      row.append(label, inspect);
      if (!state.first && !state.ownerSeen.has(message.id) && state.motion) row.animate([{opacity: 0, transform: "translateX(-22px)"}, {opacity: 1, transform: "none"}], {duration: 700, easing: "cubic-bezier(.2,.7,.2,1)"});
      state.ownerSeen.add(message.id);
      return row;
    });
    $("questions").replaceChildren.apply($("questions"), questions.length ? questions : [node("p", "No queued owner messages in this observation.", "empty")]);
    $("questions").hidden = !!attentionView;
    if (!attentionView) $("questions").parentNode.classList.toggle("is-empty", !questions.length && $("owner-detail").hidden);
    const asking = questionsOf(fleet).length > 0;
    $("seat-line").classList.toggle("hold", asking && !fleet.stale);
    $("seat-line").classList.toggle("stale", !!fleet.stale);
    const seats = fleet.seats.map(function (seat) {
      const b = button("", function () { selectedChanged(fleet.id, seat.name); });
      b.className = "seat " + (seat.state === "working" && seat.waiting ? "waiting" : seat.state);
      b.setAttribute("aria-pressed", String(state.seat === seat.name && state.targetFleet === fleet.id));
      b.dataset.focusKey = "seat:" + fleet.id + ":" + seat.name; b.dataset.seat = seat.name;
      const card = node("span", "", "card");
      card.append(node("strong", seat.name), node("small", seat.kind + " · " + seat.state + (seat.waiting ? " · waiting on its inbox" + (data.demo ? " (authored)" : "") : ""), "state"),
        node("small", seat.queued + " queued · " + seat.fyi + " FYI"));
      const activity = state.threads.get([state.fleet, fleet.id, seat.name].join("|"));
      if (activity && activity.unseen) card.append(node("small", activity.unseen + " new", "seat-new"));
      if (seat.context) card.append(node("small", String(seat.context.tokens) + (seat.context.window ? "/" + seat.context.window : "") + " context tokens"));
      if (seat.limited) card.append(node("small", "Recorded provider limit · delivery may be held"));
      b.append(lamp(seat.state), card);
      return b;
    });
    $("seats").replaceChildren.apply($("seats"), seats);
    const target = currentSeat();
    const demoHold = !!(data.demo && data.hold && state.reply);
    $("target-label").textContent = target ? target.name + " · " + target.state : "Choose a seat";
    $("watch").disabled = !target || !target.view || fleet.stale;
    $("send").disabled = !target || !target.view || fleet.stale || (targetFleet() && targetFleet().stale) || (data.demo && !demoHold) ||
      !!(state.intent && (state.intent.pending || (state.intent.result && state.intent.result.certainty === "confirmed")));
    $("new-attempt").disabled = !!(state.intent && state.intent.pending);
    $("send").textContent = demoHold ? "Record sample reply · Demo" : "Send owner message";
    $("send-hint").textContent = "Ctrl+Enter (Windows/Linux) or Command+Enter (Mac) " +
      (data.demo ? "records a sample reply only; nothing is sent live." : "sends.");
    $("read-question").disabled = data.demo || !state.question;
    $("compose-target").textContent = (state.reply ? "Reply #" + state.reply.slice(-6) + " → " : "To ") + (target ? target.name : "no selected seat") +
      (demoHold ? " · DEMO: recorded in this browser only. Nothing is sent, and the authored run continues as scripted."
        : data.demo ? " · Demo does not send live messages." : " · owner attribution · ordinary guarded delivery");
    $("clear-reply").hidden = !state.reply && !savedReply();
    $("clear-reply").textContent = state.reply ? "New message" : "Continue saved reply draft";
    $("read-with-reply-label").hidden = !state.reply || data.demo;
    $("composer-title").parentNode.parentNode.classList.toggle("replying", !!state.reply);
    $("demo-stages").hidden = !data.demo;
    $("demo-answer").hidden = !(data.demo && data.hold);
    if (data.demo) {
      const evidence = data.stage_evidence || [];
      $("demo-stages").replaceChildren.apply($("demo-stages"), data.stages.map(function (name, index) {
        const kind = index < data.stage ? "done" : index > data.stage ? "todo" : data.hold ? "hold" : "current";
        const item = node("li", "", "demo-stage " + kind);
        item.append(node("span", "", "node"), node("span", "DEMO " + (index + 1) + " · " + name, "k"),
          node("span", kind === "done" ? (evidence[index] ? "closed on #" + evidence[index].slice(-6) : "closed") : kind === "hold" ? "waiting for you" : kind === "current" ? "under way" : "", "sub"));
        return item;
      }));
    }
    logBadge();
    if (state.streamKey && (!target || state.streamKey !== target.view)) disconnect("Seat binding changed; refresh and reconnect explicitly.");
    if (focusedKey) {
      const replacement = Array.from(document.querySelectorAll("[data-focus-key]")).find(function (item) { return item.dataset.focusKey === focusedKey; });
      if (replacement) replacement.focus({preventScroll: true});
    }
    renderConversation();
    renderAttention();
  }
  async function refresh() {
    if (!state.csrf || document.hidden) return;
    const epoch = state.authEpoch;
    try {
      const previous = state.data; const data = await (await api("/api/snapshot")).json();
      if (epoch !== state.authEpoch) return;
      state.data = data;
      if (!data.fleets.some(function (f) { return f.id === state.fleet; })) {
        saveDraft(); if (state.fleet) disconnect("Registered fleet unavailable.");
        state.fleet = data.fleets[0] ? data.fleets[0].id : null; state.targetFleet = state.fleet;
        state.seat = data.fleets[0] && data.fleets[0].seats[0] ? data.fleets[0].seats[0].name : null;
        restoreDraft();
      }
      render();
      const events = [];
      data.fleets.forEach(function (f) { (f.events || []).forEach(function (e) {
        const key = [e.id, e.kind, e.time, e.from, e.to].join("|");
        if (!state.first && !state.seen.has(key)) events.push([f.id, e]);
        state.seen.add(key);
      }); });
      while (state.seen.size > 1024) state.seen.delete(state.seen.values().next().value);
      if (events.length) {
        const last = events[events.length - 1][1];
        notice(last.from + " → " + last.to + " · " + (last.delivery || last.kind) + " · #" + last.id.slice(-6));
        const sent = new Set();
        events.forEach(function (pair) {
          const fleetId = pair[0], e = pair[1];
          state.unseen.set(fleetId, (state.unseen.get(fleetId) || 0) + 1);
          const peerName = e.from === "owner" ? e.to : e.to === "owner" ? e.from :
            e.from === "herdr-fleet" && e.kind === "delivered" ? e.to : null;
          const sourceFleet = data.fleets.find(function (f) { return f.id === fleetId; });
          if (peerName && sourceFleet && sourceFleet.seats.some(function (seat) { return seat.name === peerName; })) {
            const key = [fleetId, fleetId, peerName].join("|");
            if (key !== threadKey() && !state.threads.has(key)) thread(key);
          }
          state.threads.forEach(function (item, key) {
            const identity = key.split("|");
            if (key === threadKey() || identity[0] !== fleetId) return;
            const peer = identity[2];
            if ((e.from === peer && e.to === "owner") || (e.from === "owner" && e.to === peer) ||
                (e.from === "herdr-fleet" && e.to === peer && e.kind === "delivered")) item.unseen = Math.min(999, item.unseen + 1);
          });
          if (fleetId !== state.fleet) return;
          if (e.kind === "send") { sent.add(e.id); travel(e, e.delivery === "queued" ? "queued" : null); }
          else if (e.kind === "delivered") {
            if (sent.has(e.id) && state.motion) setTimeout(function () { ring(e.to, "delivered"); }, 1300); else ring(e.to, "delivered");
          }
        });
        bump($("log-count")); render();
      }
      if (!state.first && previous) {
        const before = new Map();
        previous.fleets.forEach(function (f) { f.seats.forEach(function (x) { before.set(f.id + "|" + x.name, x.state); }); });
        const shown = currentFleet();
        if (shown) shown.seats.forEach(function (x) {
          const old = before.get(shown.id + "|" + x.name);
          if (old !== undefined && old !== x.state) ring(x.name, x.state === "working" ? "working" : x.state === "blocked" ? "blocked" : "state");
        });
      }
      await Promise.all([refreshConversation(), refreshAttention()]);
      state.first = false;
      if (state.tab !== "terminal") {
        const historyKey = threadKey();
        try { await refreshTab(); }
        catch (error) { if (epoch === state.authEpoch && historyKey === threadKey()) notice("History unavailable. " + error.message); }
      }
      state.quiet = false;
    } catch (error) {
      if (epoch === state.authEpoch) {
        notice("Disconnected · last observation retained. " + error.message);
        $("freshness").textContent = "DISCONNECTED · last observation retained";
        state.conversationRequest++;
        state.attentionRequest++;
        state.attention = Object.assign({}, state.attention || {items: [], counts: {}}, {stale: true, incomplete: true});
        renderAttention();
        if (state.data && state.seat) {
          const item = thread(); item.state = "stale"; item.note = "Disconnected · last conversation retained. " + error.message;
          renderConversation();
        }
      }
    }
  }
  async function list(mode, name, id, fleet) {
    let query = "?fleet=" + encodeURIComponent(fleet || state.fleet) + "&mode=" + mode + "&limit=50";
    if (name) query += "&name=" + encodeURIComponent(name);
    if (id) query += "&id=" + encodeURIComponent(id);
    return (await api("/api/list" + query)).json();
  }
  async function inspectQuestion(message) {
    const epoch = state.authEpoch, fleet = state.fleet, request = ++state.detailRequest;
    const result = await list("messages", "owner", message.id);
    if (epoch !== state.authEpoch || fleet !== state.fleet || request !== state.detailRequest) return;
    const item = result.items[0]; if (!item) throw new Error("Owner message unavailable; refresh.");
    if (item.id !== message.id || item.to !== "owner") throw new Error("Owner message identity changed; refresh.");
    state.question = item; $("question-sender").textContent = item.sender + " · #" + item.id.slice(-6) + " · " + item.box + "/";
    $("question-body").textContent = (item.body_available === false ? "Message body unavailable; inspect the recorded metadata." : item.body) + (item.body_truncated ? "\n[Body truncated]" : "");
    $("owner-detail").hidden = false; $("read-question").disabled = state.data.demo;
    const choices = replyTargets(item.sender);
    $("reply-question").disabled = choices.length !== 1 || item.body_available === false || !!item.incomplete;
    if (!$("reply-question").disabled) $("reply-question").focus();
    else { $("question-body").tabIndex = -1; $("question-body").focus(); }
  }
  function replyTargets(sender) {
    const matches = [];
    state.data.fleets.forEach(function (fleet) {
      if (fleet.stale) return;
      fleet.seats.forEach(function (seat) { if (seat.name === sender && seat.view) matches.push([fleet.id, seat.name]); });
    });
    return matches;
  }
  async function refreshTab() {
    const epoch = state.authEpoch, key = threadKey(), tab = state.tab, mailbox = state.messageMailbox;
    const request = ++state.historyRequest;
    const current = function () { return epoch === state.authEpoch && key === threadKey() && tab === state.tab &&
      mailbox === state.messageMailbox && request === state.historyRequest; };
    if (state.tab === "log") {
      state.unseen.set(state.fleet, 0); logBadge();
      const result = await list("log");
      if (!current()) return;
      const lines = result.items.map(function (e) { return [e.time, e.from + " → " + e.to, e.kind, e.delivery || e.via || "", e.id ? "#" + e.id.slice(-6) : "", e.first_line || ""].join("  "); });
      $("log-output").textContent = lines.join("\n") + (result.truncated ? "\n[Recent page truncated; inspect the CLI log for older entries.]" : "");
    } else if (state.tab === "messages") {
      const result = await list("messages", state.messageMailbox === "owner" ? "owner" : state.seat, null,
                                state.messageMailbox === "owner" ? state.fleet : state.targetFleet);
      if (!current()) return;
      const children = result.items.map(function (m) {
        const item = node("article", "", "message-item");
        item.append(node("strong", m.sender + " → " + m.to + " · #" + m.id.slice(-6) + " · " + m.box + "/"),
          node("pre", m.body + (m.body_truncated ? "\n[Body truncated]" : ""), "message-body"));
        return item;
      });
      if (!children.length) children.push(node("p", "No stored messages in this page.", "empty"));
      if (result.truncated) children.push(node("p", "Recent page truncated; older history is available through the CLI.", "empty"));
      $("message-output").replaceChildren.apply($("message-output"), children);
    }
  }
  function tab(name) {
    state.tab = name;
    ["terminal", "log", "messages"].forEach(function (n) { $("tab-" + n).setAttribute("aria-selected", String(n === name)); $(n + "-panel").hidden = n !== name; });
    logBadge();
    if (name !== "terminal") disconnect("Watch disconnected while the terminal is hidden.");
    else scheduleViewport();
    refreshTab().catch(fail);
  }
  function disconnect(message) {
    if (state.watch) state.watch.abort(); state.watch = null; state.streamKey = null;
    status(message || "Watch disconnected. Output retained.");
  }
  async function watch() {
    const target = currentSeat(); if (!target || !target.view) return;
    disconnect(); term.reset();
    fit.fit();
    const dims = fit.proposeDimensions() || {cols: 80, rows: 24};
    const controller = new AbortController(); state.watch = controller; state.streamKey = target.view;
    state.viewport = {cols: Math.max(20, Math.min(300, dims.cols)), rows: Math.max(5, Math.min(120, dims.rows))};
    status("Connecting Watch · read-only viewport · handle native dialogs in Herdr");
    const key = target.view;
    try {
      const response = await api("/api/watch", {fleet: state.targetFleet, seat: target.name, view: target.view,
        cols: state.viewport.cols, rows: state.viewport.rows}, controller.signal);
      const reader = response.body.getReader(); const decoder = new TextDecoder(); let pending = "";
      while (true) {
        const next = await reader.read(); if (next.done) break;
        pending += decoder.decode(next.value, {stream: true});
        if (pending.length > 1048576) throw new Error("Watch buffer limit exceeded.");
        let index;
        while ((index = pending.indexOf("\n")) >= 0) {
          const line = pending.slice(0, index); pending = pending.slice(index + 1);
          if (!line) continue;
          const frame = JSON.parse(line);
          if (state.streamKey !== key || state.watch !== controller) return;
          if (frame.type === "frame") {
            if (frame.full) { term.reset(); }
            if (term.cols !== frame.cols || term.rows !== frame.rows) term.resize(frame.cols, frame.rows);
            const raw = atob(frame.bytes); const bytes = new Uint8Array(raw.length);
            for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
            term.write(bytes);
            $("terminal-status").textContent = (state.data.demo ? "DEMO · synthetic ANSI" : "WATCH CONNECTED · read only") +
              " · " + target.name + " · frame " + frame.seq + (target.state === "blocked" ? " · Handle this native dialog in Herdr" : "");
          } else {
            status(frame.message || "Watch stream changed; reconnect explicitly.");
          }
        }
      }
      if (state.watch === controller) { state.watch = null; state.streamKey = null; status($("terminal-status").textContent + (state.data.demo ? " · Demo stream closed" : " · stream ended; agent exit is not inferred")); }
    } catch (error) {
      if (state.watch === controller && error.name !== "AbortError") { disconnect("Watch unavailable or disconnected · " + error.message); }
    }
  }
  function uniqueIntent() { return crypto.randomUUID(); }
  let resizeTimer = null;
  function scheduleViewport() {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function () {
      if (state.tab !== "terminal") return;
      const dims = fit.proposeDimensions();
      if (!dims) return;
      const cols = Math.max(20, Math.min(300, dims.cols)), rows = Math.max(5, Math.min(120, dims.rows));
      if (state.watch && state.viewport && (cols !== state.viewport.cols || rows !== state.viewport.rows)) {
        status("Watch reconnecting for a new read-only viewport; fresh full frame required.");
        watch();
      } else if (!state.watch) fit.fit();
    }, 200);
  }
  new ResizeObserver(scheduleViewport).observe($("terminal-screen"));
  async function send() {
    const target = currentSeat(); if (!target) return;
    if (state.data.demo) return demoReply();
    const text = $("draft").value;
    if (!text.trim()) { notice("Enter a plain-text message first."); return; }
    const body = {action: "send", source: state.fleet, fleet: state.targetFleet, seat: target.name,
      view: target.view, text: text};
    if (state.reply) { body.reply_to = state.reply; body.read = $("read-with-reply").checked; }
    const fingerprint = JSON.stringify(body);
    if (state.intent && state.intent.fingerprint !== fingerprint) {
      notice("This draft has a pending or uncertain intent. Inspect receipts, then prepare a deliberate new attempt."); return;
    }
    if (!state.intent) state.intent = {id: uniqueIntent(), fingerprint: fingerprint};
    if (state.intent.pending || (state.intent.result && state.intent.result.certainty === "confirmed")) return;
    // The retained object belongs to this exact draft, even when the owner changes
    // fleet/seat/reply or edits text before the HTTP response arrives.
    const origin = draftKey(), intent = state.intent;
    intent.pending = true;
    body.intent = intent.id; saveDraft(); $("send").disabled = true; $("new-attempt").disabled = true;
    const conversation = thread(), captured = state.drafts.get(origin);
    let record = conversation.local.get(intent.id);
    if (!record) {
      record = {intent: intent, draftKey: origin, generation: captured.generation,
        threadKey: threadKey(),
        replyGeneration: state.replyGeneration, readGeneration: captured.readGeneration,
        seat: target.name, text: text, time: new Date().toISOString(), reply: state.reply,
        pending: true, result: null};
      conversation.local.set(intent.id, record);
    }
    record.pending = true; renderConversation();
    try {
      const result = await (await api("/api/action", body)).json();
      if (!record.retired) record.result = result;
      intent.result = result;
      intent.description = target.name + " · " + result.status + (result.id ? " · #" + result.id.slice(-6) : "") +
        " · " + result.certainty + (result.replayed ? " · existing intent result" : "") + " · delivery is not work acceptance";
      retire(record, record.result);
      await refresh();
    } catch (error) {
      if (!record.retired) {
        record.result = {status: "unconfirmed", certainty: "unconfirmed", message: "Result uncertain; inspect receipts before an explicit new attempt."};
        intent.description = target.name + " · Result uncertain or rejected · " + error.message + ". Draft and intent retained.";
      }
    } finally {
      intent.pending = false; record.pending = false;
      if (draftKey() === origin && state.intent === intent) {
        $("action-result").textContent = intent.description;
        $("new-attempt").hidden = false;
      }
      render(); renderConversation();
    }
  }
  async function demoReply() {
    if (!state.data.demo || !state.data.hold || !state.reply || state.demoPending) return;
    const typed = $("draft").value.trim().length;
    if (!typed) { notice("Type a sample reply first. It stays in this browser."); return; }
    saveDraft();
    const origin = draftKey(), reply = state.reply, generation = state.drafts.get(origin).generation;
    const replyGeneration = state.replyGeneration, item = thread(), text = $("draft").value, seat = state.seat;
    state.demoPending = true;
    $("send").disabled = true;
    try {
      await (await api("/api/demo/reply", {})).json();
      item.local.set("demo:" + uniqueIntent(), {demo: true, seat: seat, reply: reply, text: text,
        time: new Date().toISOString(), result: {status: "local", certainty: "confirmed"}, pending: false});
      if (draftKey() === origin && state.drafts.get(origin).generation === generation) {
        $("action-result").textContent = "DEMO · sample reply recorded in this browser only (" + typed + " characters). Nothing was sent. The authored run continues.";
        $("draft").value = ""; saveDraft();
        if (state.replyGeneration === replyGeneration) { state.reply = null; state.replyGeneration++; saveDraft(); }
        if (state.question && state.question.id === reply) { state.question = null; $("owner-detail").hidden = true; }
      }
      await refresh();
    } catch (error) { if (draftKey() === origin) $("action-result").textContent = "DEMO · the Demo server did not accept the reply action · " + error.message; render(); }
    finally { state.demoPending = false; renderConversation(); }
  }
  $("demo-answer").addEventListener("click", function () {
    const fleet = state.data.fleets.find(function (f) { return questionsOf(f).length; }); if (!fleet) return;
    selectFleet(fleet.id);
    inspectQuestion(questionsOf(fleet)[0]).then(function () {
      $("reply-question").click();
      if (!$("draft").value) { $("draft").value = "Fix the two tests first, then merge."; saveDraft(); }
      render(); $("draft").focus();
    }).catch(fail);
  });
  $("collapse").addEventListener("click", function () {
    state.collapsed = !state.collapsed; $("app").classList.toggle("collapsed", state.collapsed);
    $("collapse").textContent = state.collapsed ? "Expand fleets" : "Collapse fleets";
    $("collapse").setAttribute("aria-expanded", String(!state.collapsed));
    saveView();
  });
  $("expand").addEventListener("click", function () {
    state.expanded = !state.expanded; $("app").classList.toggle("expanded", state.expanded);
    $("expand").textContent = state.expanded ? "Restore terminal" : "Expand terminal";
    $("expand").setAttribute("aria-expanded", String(state.expanded));
    $("esc-hint").hidden = !state.expanded;
    $("grip").setAttribute("aria-disabled", String(state.expanded)); $("grip").tabIndex = state.expanded ? -1 : 0;
    scheduleViewport();
  });
  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape" || !state.expanded) return;
    const tag = event.target && event.target.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
    $("expand").click(); $("expand").focus();
  });
  function motion() {
    document.body.classList.toggle("motion-off", !state.motion);
    $("motion").setAttribute("aria-checked", String(state.motion));
    $("motion-state").textContent = state.motion ? "on" : "off";
    if (!state.motion) $("travel").replaceChildren();
  }
  $("motion").addEventListener("click", function () { state.motion = !state.motion; state.motionChosen = true; motion(); saveView(); }); motion();
  $("view-settings").addEventListener("click", function () {
    $("view-prefs").hidden = !$("view-prefs").hidden;
    $("view-settings").setAttribute("aria-expanded", String(!$("view-prefs").hidden));
  });
  $("reset-view-preferences").addEventListener("click", function () {
    state.preferenceWarning = false;
    try { if (preferenceKey()) localStorage.removeItem(preferenceKey()); }
    catch (error) { state.preferenceWarning = true; }
    applyView(defaultView()); state.attention = null; state.attentionRequest++; preferenceStatus(); renderAttention();
  });
  matchMedia("(prefers-reduced-motion: reduce)").addEventListener("change", function (event) {
    if (!state.motionChosen) { state.motion = !event.matches; motion(); }
  });
  $("replay").addEventListener("click", function () {
    $("travel").replaceChildren(); state.ownerSeen.clear();
    state.threads.forEach(function (item) { item.local.forEach(function (record, key) { if (record.demo) item.local.delete(key); }); });
    $("action-result").textContent = "A confirmed delivery is not acceptance of work.";
    api("/api/demo/replay", {}).then(function (response) { return response.json(); }).then(refresh).catch(fail);
  });
  ["terminal", "log", "messages"].forEach(function (name) { $("tab-" + name).addEventListener("click", function () { tab(name); }); });
  ["terminal", "log", "messages"].forEach(function (name, index, names) {
    $("tab-" + name).addEventListener("keydown", function (event) {
      if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
      event.preventDefault();
      const next = names[(index + (event.key === "ArrowRight" ? 1 : 2)) % names.length];
      tab(next); $("tab-" + next).focus();
    });
  });
  $("watch").addEventListener("click", watch);
  $("refresh-observation").addEventListener("click", refresh);
  $("disconnect").addEventListener("click", function () { disconnect(); });
  $("refit").addEventListener("click", function () { if (state.watch) watch(); else notice("Open Watch before refitting its read-only viewport."); });
  $("copy").addEventListener("click", function () {
    const lines = []; for (let i = 0; i < term.buffer.active.length; i++) lines.push(term.buffer.active.getLine(i).translateToString(true));
    navigator.clipboard.writeText(lines.join("\n")).then(function () { notice("Copied terminal screen text."); }).catch(fail);
  });
  $("refresh-log").addEventListener("click", function () { refreshTab().catch(fail); });
  $("watch-log").addEventListener("click", function () { list("watch-log").then(function (r) { $("log-output").textContent = r.items.join("\n"); }).catch(fail); });
  $("owner-history").addEventListener("click", function () { state.messageMailbox = "owner"; refreshTab().catch(fail); });
  $("seat-history").addEventListener("click", function () { state.messageMailbox = "seat"; refreshTab().catch(fail); });
  $("refresh-messages").addEventListener("click", function () { refreshTab().catch(fail); });
  $("close-question").addEventListener("click", function () { $("owner-detail").hidden = true; state.question = null; render(); });
  $("reply-question").addEventListener("click", function () {
    if (!state.question) return;
    const matches = replyTargets(state.question.sender);
    if (matches.length !== 1) { notice("The question sender is not a unique registered seat."); return; }
    selectedChanged(matches[0][0], matches[0][1]); chooseReply(state.question.id); render(); $("draft").focus();
  });
  $("read-question").addEventListener("click", async function () {
    if (!state.question || state.data.demo) return;
    const button = $("read-question"); button.disabled = true;
    try {
      const result = await (await api("/api/action", {action: "read", source: state.fleet,
        message: state.question.id, intent: uniqueIntent()})).json();
      notice("Selected owner message: " + result.status + " · reading does not acknowledge it."); await refresh();
    } catch (error) { fail(error); } finally { button.disabled = state.data.demo; }
  });
  $("clear-reply").addEventListener("click", function () { chooseReply(state.reply ? null : savedReply()); render(); });
  $("read-with-reply").addEventListener("change", function () { saveDraft(); });
  $("draft").addEventListener("input", saveDraft);
  $("draft").addEventListener("keydown", function (event) {
    if (event.key !== "Enter" || !(event.metaKey || event.ctrlKey) || event.altKey || event.shiftKey ||
        event.repeat || event.isComposing || event.keyCode === 229 || $("send").disabled) return;
    event.preventDefault();
    $("send").click();
  });
  $("send").addEventListener("click", send);
  $("new-attempt").addEventListener("click", function () {
    if (state.intent && state.intent.pending) return;
    state.intent = null; saveDraft(); $("new-attempt").hidden = true;
    notice("New attempt prepared. Verify previous receipts before pressing Send."); render();
  });
  let drag = null;
  function height(value) {
    const pixels = Number.isFinite(value) ? Math.round(Math.max(200, Math.min(900, value))) : 360;
    state.terminalHeight = pixels;
    $("terminal-screen").style.setProperty("--terminal-height", pixels + "px");
    $("grip").setAttribute("aria-valuenow", String(pixels));
  }
  $("grip").addEventListener("pointerdown", function (event) { if (state.expanded) return; drag = {y: event.clientY, height: $("terminal-screen").clientHeight}; $("grip").setPointerCapture(event.pointerId); });
  $("grip").addEventListener("pointermove", function (event) { if (drag && !state.expanded) height(drag.height + event.clientY - drag.y); });
  $("grip").addEventListener("pointerup", function () { drag = null; saveView(); });
  $("grip").addEventListener("keydown", function (event) { if (state.expanded) return; if (event.key === "ArrowUp" || event.key === "ArrowDown") { event.preventDefault(); height($("terminal-screen").clientHeight + (event.key === "ArrowUp" ? -24 : 24)); saveView(); } });
  document.addEventListener("visibilitychange", function () { if (!document.hidden) { state.first = true; state.quiet = true; refresh(); } });
  window.addEventListener("pagehide", function () { disconnect(); clearInterval(state.tick); clearTimeout(resizeTimer); if (conversationView) conversationView.destroy(); if (attentionView) attentionView.destroy(); });
  async function bootstrap(supplied) {
    const launch = supplied || capability;
    capability = null;
    if (!launch) { notice("Owner session required. Reopen the complete launch URL printed by this dashboard process."); return; }
    const epoch = ++state.authEpoch;
    clearInterval(state.tick); state.tick = null; state.csrf = null;
    disconnect("Watch disconnected: reauthenticating owner session.");
    const response = await fetch("/api/session", {method: "POST", credentials: "same-origin", cache: "no-store",
      headers: {"Content-Type": "application/json"}, body: JSON.stringify({capability: launch})});
    const result = await response.json(); if (!response.ok) throw new Error(result.error || "Session exchange failed.");
    if (epoch !== state.authEpoch) return;
    state.csrf = result.csrf; notice(result.demo ? "DEMO · Synthetic authored content. No live APIs or owner sends." : "Owner session established. Live observations do not mark messages read.");
    await refresh(); if (epoch === state.authEpoch) state.tick = setInterval(refresh, 2000);
  }
  window.addEventListener("hashchange", function () {
    const pasted = location.hash.slice(1);
    history.replaceState(null, "", location.pathname);
    if (!pasted) return;
    return bootstrap(pasted).catch(fail);
  });
  bootstrap().catch(function (error) { capability = null; fail(error); });
}());
