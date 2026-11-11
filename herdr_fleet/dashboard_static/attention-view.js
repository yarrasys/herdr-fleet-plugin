/* Decision Desk: inert, bounded supplied metadata. app.js owns every effect. */
"use strict";
(function (global) {
  const TYPES = new Map([
    ["owner_message", ["Owner message", "question"]], ["native_dialog", ["Native dialog", "dialog"]],
    ["send_issue", ["Send issue", "send"]], ["provider_hold", ["Provider hold", "limit"]],
    ["observation_issue", ["Observation", "stale"]]
  ]);
  const REASONS = new Map([
    ["owner_pending", "Unread owner message"], ["owner_awaiting_reply", "Delivered request awaiting reply"],
    ["owner_header_unavailable", "Message evidence unavailable; no reply route"],
    ["native_dialog_in_herdr", "Observed blocked. Handle the dialog in Herdr."],
    ["send_unconfirmed", "Recorded send outcome unconfirmed"], ["send_refused", "Recorded refused outcome"],
    ["send_expired", "Recorded expired outcome"], ["send_dropped", "Recorded dropped outcome"],
    ["active_provider_hold", "Recorded active provider hold"], ["fleet_observation_stale", "Last fleet observation is stale"],
    ["watcher_stopped", "Watcher confirmed stopped"], ["watcher_liveness_unknown", "Watcher liveness unknown"],
    ["source_metadata_incomplete", "Source metadata is incomplete"]
  ]);
  const ACTIONS = new Map([["inspect_owner", "Inspect"], ["watch", "Watch read-only"],
    ["inspect_send", "Open conversation"], ["inspect_seat", "Inspect hold"],
    ["inspect_log", "Inspect receipts"], ["refresh", "Refresh observation"]]);
  const STATUSES = new Set(["unread", "awaiting_reply", "unavailable", "blocked", "unconfirmed", "refused", "expired", "dropped", "held", "stale", "stopped", "unknown", "incomplete"]);
  const text = function (value) { return value === null || value === undefined ? "" : String(value).slice(0, 256); };
  function el(tag, cls, words) {
    const node = document.createElement(tag); if (cls) node.className = cls;
    if (words !== undefined) node.textContent = text(words); return node;
  }
  function say(node, words) { if (node.textContent !== words) node.textContent = words; }
  function count(value) { return Number.isInteger(value) && value >= 0 ? value : 0; }
  function age(value) {
    const when = value && Date.parse(value);
    if (!Number.isFinite(when)) return "Time unknown";
    const seconds = Math.max(0, Math.floor((Date.now() - when) / 1000));
    return seconds < 60 ? seconds + "s ago" : Math.floor(seconds / 60) + "m ago";
  }
  function mount(root, callbacks) {
    const rows = new Map(), scopes = new Map(), chips = new Map();
    let model = null, folded = false, previous = null;
    const head = el("div", "desk-head"), total = el("span", "desk-count", "0");
    const title = el("h3", "", "Needs attention"); title.id = "attention-title"; title.tabIndex = -1;
    const segment = el("div", "seg"); segment.setAttribute("aria-label", "Attention fleet scope");
    ["all", "selected"].forEach(function (scope) {
      const button = el("button", "", scope === "all" ? "All fleets" : "Selected fleet"); button.type = "button";
      button.dataset.scope = scope;
      button.addEventListener("click", function () { if (model) callbacks.onFilter(scope, model.category); });
      scopes.set(scope, button); segment.append(button);
    });
    const fold = el("button", "desk-fold", "Hide list"); fold.type = "button";
    fold.setAttribute("aria-controls", "attention-list"); fold.setAttribute("aria-expanded", "true");
    const filters = el("div", "chips"); filters.setAttribute("aria-label", "Attention category");
    [["all", ["All", ""]]].concat(Array.from(TYPES)).forEach(function (pair) {
      const category = pair[0], button = el("button", "chip"); button.type = "button";
      button.dataset.category = category;
      const label = el("span", "", pair[1][0]), number = el("b", "", "0"); button.append(label, number);
      button.addEventListener("click", function () { if (model) callbacks.onFilter(model.scope, category); });
      chips.set(category, {button: button, number: number}); filters.append(button);
    });
    const list = el("ol", "desk-list"); list.id = "attention-list"; list.setAttribute("aria-label", "Observed attention items");
    const empty = el("p", "desk-clear", "No observed matching items; this does not establish completion.");
    const foot = el("p", "desk-foot"), live = el("p", "sr-only");
    live.setAttribute("role", "status"); live.setAttribute("aria-live", "polite");
    fold.addEventListener("click", function () {
      folded = !folded; list.hidden = folded; empty.hidden = folded || !!(model && model.items.length);
      fold.textContent = folded ? "Show list" : "Hide list"; fold.setAttribute("aria-expanded", String(!folded));
    });
    head.append(total, title, segment, fold, filters); root.replaceChildren(head, list, empty, foot, live);
    function create(id) {
      const row = el("li", "desk-row"), mark = el("span", "mark"), kind = el("strong", "desk-kind");
      row.dataset.attentionId = id; mark.setAttribute("aria-hidden", "true");
      const where = el("span", "desk-where"), why = el("span", "desk-why"), when = el("span", "desk-age");
      const action = el("button"); action.type = "button";
      action.addEventListener("click", function () { callbacks.onAction(id); });
      row.append(mark, kind, where, why, when, action);
      return {row: row, mark: mark, kind: kind, where: where, why: why, when: when, action: action};
    }
    return {
      render: function (next) {
        model = next;
        const items = Array.isArray(next.items) ? next.items.slice(0, 200) : [];
        const counts = next.counts || {}, capped = next.counts_capped ? "+" : "";
        const matched = count(next.matched_count === undefined ? items.length : next.matched_count);
        say(total, String(matched) + capped);
        scopes.forEach(function (button, scope) { button.setAttribute("aria-pressed", String(scope === next.scope)); });
        chips.forEach(function (chip, category) {
          const number = category === "all" ? Array.from(TYPES.keys()).reduce(function (n, key) { return n + count(counts[key]); }, 0) : count(counts[category]);
          say(chip.number, String(number) + capped); chip.button.setAttribute("aria-pressed", String(category === next.category));
        });
        const labels = new Map((next.fleets || []).map(function (fleet) { return [fleet.id, text(fleet.label)]; }));
        const oldFocus = document.activeElement, retained = new Set();
        items.forEach(function (item, index) {
          const id = text(item.id); if (!id || retained.has(id)) return; retained.add(id);
          const entry = rows.get(id) || create(id); rows.set(id, entry);
          const type = TYPES.get(item.type) || [text(item.type) || "Unknown type", "stale"];
          entry.row.className = "desk-row k-" + type[1]; entry.mark.className = "mark " + type[1];
          say(entry.kind, type[0]);
          say(entry.where, (labels.get(item.fleet) || "Registered fleet") + " · " + text(item.sender || item.seat || "Observation"));
          say(entry.why, (REASONS.get(item.reason) || text(item.reason) || "Evidence unknown") +
            (item.message ? " · #" + text(item.message).slice(-6) : "") + (item.incomplete ? " · evidence incomplete" : "") +
            (item.type === "provider_hold" ? " · reset " + (item.resets_at && Number.isFinite(Date.parse(item.resets_at)) ? new Date(item.resets_at).toLocaleTimeString() : "unknown") : "") +
            (!STATUSES.has(item.status) ? " · status " + text(item.status) + " (unconfirmed)" : ""));
          say(entry.when, age(item.time));
          const action = Array.isArray(item.actions) && item.actions.length === 1 ? ACTIONS.get(item.actions[0].kind) : null;
          say(entry.action, action || "Unavailable"); entry.action.disabled = !action ||
            (!!next.stale && item.actions[0].kind !== "refresh") || !STATUSES.has(item.status);
          entry.action.setAttribute("aria-label", (action || "Unavailable") + " · " + type[0] + " · " + text(item.sender || item.seat || labels.get(item.fleet)));
          if (list.children[index] !== entry.row) list.insertBefore(entry.row, list.children[index] || null);
        });
        let removedFocus = false;
        rows.forEach(function (entry, id) {
          if (!retained.has(id)) { if (entry.row.contains(oldFocus)) removedFocus = true; entry.row.remove(); rows.delete(id); }
        });
        if (removedFocus) title.focus({preventScroll: true});
        else if (root.contains(oldFocus) && document.activeElement !== oldFocus) oldFocus.focus({preventScroll: true});
        list.hidden = folded; empty.hidden = folded || items.length > 0;
        say(foot, next.stale ? "Disconnected · last attention observation retained." :
          (next.incomplete ? "Some source evidence is incomplete. " : "") +
          (next.truncated ? "Showing a bounded page; counts may be lower bounds. " : "") +
          "Observed metadata only. Inspecting does not read or acknowledge mail.");
        const signature = JSON.stringify([items.map(function (i) { return [i.id, i.status, i.reason, i.incomplete]; }), counts, !!next.incomplete, !!next.stale]);
        if (previous !== null && signature !== previous && !next.quiet) say(live, "Attention observation updated: " + matched + capped + " matching items.");
        previous = signature;
      },
      destroy: function () { rows.clear(); root.replaceChildren(); model = null; }
    };
  }
  global.AttentionView = {mount: mount, version: 1};
}(window));
