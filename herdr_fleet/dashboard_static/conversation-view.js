/* Signal Box conversation view (design 42). Inert rendering of a supplied thread.
 * No network, polling, send, auth or intent work happens here; the controller in app.js owns all of it.
 * Every string from the model reaches the page through textContent, never through markup. */
"use strict";
(function (global) {
  /* status word -> [label, tone]; tones are a closed set of class names, never taken from data.
   * Maps, not objects: a word such as "constructor" or "__proto__" must find nothing here. */
  const STATUS = new Map([
    ["sending", ["Sending", "pending"]], ["queued", ["Queued", "held"]], ["delivered", ["Delivered", "ok"]],
    ["unconfirmed", ["Unconfirmed", "unsure"]], ["expired", ["Expired", "bad"]], ["dropped", ["Dropped", "bad"]],
    ["refused", ["Refused", "bad"]], ["failed", ["Not sent", "bad"]], ["local", ["Recorded in this browser only", "local"]],
    ["received", ["Received", "ok"]]
  ]);
  const RECEIPT = new Map([
    ["queued", ["Queued", "held"]], ["delivered", ["Delivered", "ok"]], ["read", ["Read", "ok"]], ["ack", ["Acknowledged", "ack"]],
    ["expired", ["Expired", "bad"]], ["dropped", ["Dropped", "bad"]]
  ]);
  const STATES = new Set(["loading", "stale", "unavailable"]), OLDER = new Set(["more", "loading", "truncated"]);
  const END_SLACK = 28, LONG_BODY = 900, LONG_LINES = 14, THREADS_KEPT = 64;

  function el(tag, className, text) {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (text !== undefined && text !== null) item.textContent = String(text);
    return item;
  }
  function text(value) { return value === undefined || value === null ? "" : String(value); }
  /* Writes that change nothing are skipped, so an identical render leaves the page untouched. */
  function say(item, value) { if (item.textContent !== value) item.textContent = value; }
  function mark(item, name, value) { if (item.getAttribute(name) !== value) item.setAttribute(name, value); }
  function hide(item, hidden) { if (item.hidden !== hidden) item.hidden = hidden; }
  function shortId(id) { return "#" + text(id).slice(-6); }
  function stamp(value) {
    const date = new Date(value);
    return value && Number.isFinite(date.getTime()) ? date : null;
  }
  function clock(value) {
    const date = stamp(value); if (!date) return null;
    const item = el("time", "", date.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", second: "2-digit"}));
    item.dateTime = date.toISOString();
    return item;
  }
  /* One wording for a receipt, used both on the row and in the announcement, so neither can say less than the other. */
  function receiptWords(r) {
    const kind = RECEIPT.get(r.kind);
    if (!kind) return {label: r.kind || "Unknown record", tone: "unsure", extra: ""};
    if (r.kind === "ack") {
      return {label: kind[0], tone: r.exact ? kind[1] : "unsure",
        extra: (r.exact ? "" : "generic, not tied to this id" + (r.by ? " · " : "")) + (r.by ? "by " + r.by : "")};
    }
    return {label: kind[0], tone: kind[1], extra: r.via ? "via " + r.via : ""};
  }
  /* A result is uncertain when the controller says so. A row still being sent, or one that only ever lived in
   * this browser, has no result yet: it keeps its own pending or local look whatever certainty came with it. */
  function uncertain(row) { return row.certainty === "unconfirmed" && row.status !== "sending" && row.status !== "local"; }
  /* The status headline in words. An unknown status is its own word; an unconfirmed result always says so. */
  function statusWords(row) {
    const known = STATUS.get(row.status), unsure = uncertain(row);
    return {label: (known ? known[0] : row.status) + (row.status === "sending" ? "…" : "") +
        (unsure && row.status !== "unconfirmed" ? " · unconfirmed" : ""),
      tone: known && !unsure ? known[1] : "unsure"};
  }
  function toneOf(row) {
    if (uncertain(row)) return "unsure";
    const known = STATUS.get(row.status);
    return known ? known[1] : row.status ? "unsure" : "ok";
  }
  /* What changed on a row, in one sentence fragment: its newest receipt, its status when no receipt stands for it,
   * and the uncertainty whenever the current result is unconfirmed. */
  function summary(row) {
    const parts = [], last = row.receipts[row.receipts.length - 1];
    const matched = row.receipts.some(function (r) { return r.kind === row.status; });
    if (last) { const words = receiptWords(last); parts.push(words.label + (words.extra ? (/^(by|via) /.test(words.extra) ? " " : ", ") + words.extra : "")); }
    if (row.status && row.status !== "received" && !matched) parts.push(statusWords(row).label);
    else if (uncertain(row)) parts.push("outcome unconfirmed");
    return parts.join("; ");
  }
  function dayOf(value) {
    const date = stamp(value);
    return date ? date.toLocaleDateString([], {day: "numeric", month: "short", year: "numeric"}) : "";
  }

  /* A plain, frozen-in-time copy of one supplied row: nothing of the caller's object is kept. */
  function rowOf(raw) {
    const from = text(raw.from);
    const receipts = Array.isArray(raw.receipts) ? raw.receipts.map(function (r) {
      return {kind: text(r && r.kind), time: r && r.time ? text(r.time) : null, by: r && r.by ? text(r.by) : "",
        via: r && r.via ? text(r.via) : "", exact: !(r && r.exact === false)};
    }) : [];
    return {key: text(raw.key || raw.id), supersedes: raw.supersedes ? text(raw.supersedes) : "",
      id: raw.id ? text(raw.id) : "", from: from, to: text(raw.to), owner: from === "owner",
      time: raw.time ? text(raw.time) : null, body: text(raw.body), cut: !!raw.body_truncated, fyi: !!raw.fyi,
      replyTo: raw.reply_to ? text(raw.reply_to) : "", status: text(raw.status), certainty: text(raw.certainty),
      detail: text(raw.detail), receipts: receipts, owed: !!raw.owed, incomplete: !!raw.incomplete};
  }

  function mount(root, hooks) {
    if (!root) throw new Error("ConversationView.mount needs its element");
    hooks = hooks || {};
    const threads = new Map();          // thread key -> what the owner was looking at there
    const nodes = new Map();            // row key -> {item, sign, body}
    let thread = null, memo = null, demo = false, peer = "", rendering = false, alive = true, wasHidden = false, watcher = null, echo = -1;
    const sized = {w: 0, h: 0};         // the thread's box as last reported settled by the resize observer
    const calls = [];

    const state = el("p", "convo-state");
    const who = el("strong", "convo-peer"), note = el("span", "convo-note");
    state.append(who, note);
    const scroller = el("div", "convo-scroll");
    scroller.tabIndex = 0; scroller.setAttribute("role", "log"); scroller.setAttribute("aria-live", "polite");
    scroller.setAttribute("aria-relevant", "additions");
    const older = el("button", "convo-older"); older.type = "button"; older.hidden = true;
    const olderNote = el("p", "convo-older-note"); olderNote.hidden = true;
    const list = el("ol", "convo-list");
    const empty = el("p", "convo-empty");
    scroller.append(older, olderNote, list, empty);
    const fresh = el("button", "convo-new"); fresh.type = "button"; fresh.hidden = true;
    const spoken = el("p", "convo-spoken"); spoken.setAttribute("role", "status"); spoken.setAttribute("aria-live", "polite");
    root.replaceChildren(state, scroller, fresh, spoken);
    root.classList.add("convo-mounted");

    function remember(key) {
      if (!threads.has(key)) {
        threads.set(key, {follow: true, top: 0, anchor: null, known: new Set(), open: new Set(), unseen: 0, firstNew: "", seen: "", drawn: false});
        while (threads.size > THREADS_KEPT) threads.delete(threads.keys().next().value);
      }
      return threads.get(key);
    }
    function atEnd() { return scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight <= END_SLACK; }
    /* Every scroll the view itself makes is remembered, so its own scroll event is not read as the owner's. */
    function move(top) { scroller.scrollTop = top; echo = scroller.scrollTop; }
    function toEnd() { move(scroller.scrollHeight); }
    function visible() { return scroller.clientHeight > 0; }
    /* False while a layout change is under way: a scroll seen then is the layout's doing, not the owner's. */
    function settled() { return !watcher || (scroller.clientWidth === sized.w && scroller.clientHeight === sized.h); }
    function track() {
      memo.follow = atEnd(); memo.top = scroller.scrollTop;
      memo.anchor = memo.follow ? null : anchorNow();
    }
    /* What the owner is looking at: the first row that starts inside the view, else the row covering it.
     * Its distance from the top edge is what a later reflow puts back. */
    function anchorNow() {
      const frame = scroller.getBoundingClientRect();
      let cover = null;
      for (let i = 0; i < list.children.length; i++) {
        const box = list.children[i].getBoundingClientRect();
        if (box.bottom <= frame.top + 1) continue;
        const here = {key: list.children[i].dataset.key, offset: box.top - frame.top};
        if (box.top >= frame.top - 1) return box.top < frame.bottom ? here : cover || here;
        cover = here;
      }
      return cover;
    }
    function holdAnchor(anchor) {
      const held = anchor && nodes.get(anchor.key); if (!held) return false;
      const edge = scroller.getBoundingClientRect().top;
      move(scroller.scrollTop + held.item.getBoundingClientRect().top - edge - anchor.offset);
      return true;
    }
    function call(name, a, b) { if (typeof hooks[name] === "function") calls.push([name, a, b]); }
    function flush() {
      while (calls.length) {
        const next = calls.shift();
        try { hooks[next[0]](next[1], next[2]); } catch (error) { if (global.console) global.console.error(error); }
      }
    }
    function markSeen() {
      if (!memo || !list.lastElementChild) return;
      const last = list.lastElementChild.dataset.key;
      memo.unseen = 0;
      if (memo.seen !== last) { memo.seen = last; call("onSeen", thread, last); }
    }
    function badge() {
      const count = memo ? memo.unseen : 0;
      hide(fresh, !count);
      if (count) say(fresh, count === 1 ? "1 new message" : count + " new messages");
    }

    function station(label, tone, time, extra) {
      const item = el("li", "r t-" + tone);
      item.append(el("span", "dot"), el("span", "what", label));
      if (extra) item.append(el("span", "by", extra));
      const when = clock(time); if (when) item.append(when);
      return item;
    }
    /* Only evidence that exists is drawn. Nothing here adds a station for something that has not been recorded. */
    function receiptsOf(row, replies) {
      const line = el("ol", "rcpt"); line.setAttribute("aria-label", "Recorded receipts");
      const items = row.receipts.map(function (r) {
        const words = receiptWords(r);
        return station(words.label, words.tone, r.time, words.extra);
      });
      /* The headline: the status when no receipt stands for it. When one does but the current result is
       * unconfirmed, the uncertainty still gets its own station; a receipt must never hide it. */
      const matched = row.receipts.some(function (r) { return r.kind === row.status; });
      let head = null;
      if (row.status && row.status !== "received" && !matched) { const words = statusWords(row); head = station(words.label, words.tone, null); }
      else if (uncertain(row)) head = station("Outcome unconfirmed", "unsure", null);
      if (head) {
        let at = 0;
        while (at < row.receipts.length && row.receipts[at].kind !== "ack" && row.receipts[at].kind !== "read") at++;
        items.splice(at, 0, head);
      }
      replies.forEach(function (reply) {
        const item = station("Reply", "reply", reply.time);
        const jump = el("button", "jump", shortId(reply.id || reply.key)); jump.type = "button";
        jump.dataset.role = "jump"; jump.dataset.target = reply.key;
        jump.setAttribute("aria-label", "Show reply " + shortId(reply.id || reply.key));
        item.insertBefore(jump, item.children[2] || null);
        items.push(item);
      });
      if (!items.length) return null;
      line.append.apply(line, items);
      return line;
    }
    function signature(row, replies, newDay, first) {
      return JSON.stringify([row, replies.map(function (r) { return [r.key, r.id, r.time]; }), newDay, first, demo,
        typeof hooks.onReply === "function"]);
    }
    function paint(entry, row, replies, newDay, rows) {
      const item = entry.item;
      const active = document.activeElement && item.contains(document.activeElement) ? document.activeElement : null;
      const held = active ? {role: active.dataset.role || "", target: active.dataset.target || ""} : null;
      item.className = (row.owner ? "from-owner" : "from-agent") + " s-" + toneOf(row) +
        (row.fyi ? " fyi" : "") + (row.owed ? " owed" : "") + (memo.firstNew === row.key ? " first-new" : "");
      const parts = [];
      if (newDay) parts.push(el("p", "msg-day", newDay));
      const bubble = el("article", "msg");
      const head = el("header", "msg-head");
      head.append(el("strong", "msg-from", row.owner ? "Owner" : row.from));
      if (demo) head.append(el("span", "msg-demo", "Demo"));
      if (row.fyi) head.append(el("span", "msg-tag", "FYI"));
      const when = clock(row.time); if (when) head.append(when);
      if (row.id) head.append(el("span", "msg-id", shortId(row.id)));
      bubble.append(head);
      if (row.replyTo) {
        const original = rows.find(function (r) { return r.id === row.replyTo; });
        const words = "reply to " + (original && original.owner ? "your " : "") + shortId(row.replyTo);
        if (original) {
          const jump = el("button", "msg-reply-to jump", "↳ " + words); jump.type = "button";
          jump.dataset.role = "origin"; jump.dataset.target = original.key;
          bubble.append(jump);
        } else bubble.append(el("p", "msg-reply-to", "↳ " + words + " · not in this page"));
      }
      if (!entry.body || entry.bodyText !== row.body || entry.bodyCut !== row.cut) {
        entry.body = el("pre", "msg-text", row.body + (row.cut ? "\n[Body truncated]" : ""));
        entry.bodyText = row.body; entry.bodyCut = row.cut;
      }
      const long = row.body.length > LONG_BODY || row.body.split("\n").length > LONG_LINES;
      const open = memo.open.has(row.key);
      entry.body.classList.toggle("clamped", long && !open);
      bubble.append(entry.body);
      if (long) {
        const more = el("button", "msg-more", open ? "Show less" : "Show all"); more.type = "button";
        more.dataset.role = "more"; more.setAttribute("aria-expanded", String(open));
        bubble.append(more);
      }
      if (row.fyi && !row.owner) bubble.append(el("p", "msg-note", "FYI · the seat's own report, not a checked result"));
      const receipts = receiptsOf(row, replies); if (receipts) bubble.append(receipts);
      if (row.detail) bubble.append(el("p", "msg-detail", row.detail));
      if (row.incomplete) bubble.append(el("p", "msg-detail unsure", "Receipt history incomplete"));
      if (row.owed && !row.owner) {
        const ask = el("p", "msg-owed");
        ask.append(el("span", "lamp question"), el("span", "", "Awaits your reply"));
        if (typeof hooks.onReply === "function" && row.id) {
          const reply = el("button", "attention", "Reply"); reply.type = "button"; reply.dataset.role = "reply";
          reply.setAttribute("aria-label", "Reply to " + row.from + " " + shortId(row.id));
          ask.append(reply);
        }
        bubble.append(ask);
      }
      parts.push(bubble);
      item.replaceChildren.apply(item, parts);
      if (held) {
        /* The same control, by role and by what it points at; never a neighbour that merely shares the role. */
        const again = Array.from(item.querySelectorAll("button")).find(function (b) {
          return (b.dataset.role || "") === held.role && (b.dataset.target || "") === held.target;
        });
        (again || scroller).focus({preventScroll: true});
      }
    }

    function render(model) {
      if (!alive || rendering) return;
      rendering = true;
      try {
        model = model || {};
        const key = text(model.thread), rows = (Array.isArray(model.messages) ? model.messages : []).map(rowOf).filter(function (r) { return r.key; });
        const moved = key !== thread;
        if (moved && memo && visible() && settled()) { memo.top = scroller.scrollTop; memo.anchor = memo.follow ? null : anchorNow(); }
        if (!moved && memo && visible() && settled() && !memo.follow && !memo.unseen && atEnd()) memo.follow = true;
        const anchor = !moved && memo && !memo.follow && visible() ? (settled() ? anchorNow() : memo.anchor) : null;
        if (moved) { nodes.clear(); list.replaceChildren(); thread = key; memo = remember(key); say(spoken, ""); }
        demo = !!model.demo; peer = text(model.peer);
        /* quiet: the controller is reconciling after the page was hidden. Rows and receipts are drawn as they
         * are now; nothing enters with motion and nothing is announced, since none of it is happening now. */
        const hush = !!model.quiet;
        mark(scroller, "aria-live", hush ? "off" : "polite");
        const condition = STATES.has(model.state) ? model.state : "ready";
        mark(root, "data-state", condition); root.classList.toggle("demo", demo);
        say(who, peer ? (demo ? "Demo · " : "") + peer : "No seat selected");
        say(note, text(model.note) || (condition === "loading" ? "Loading conversation…" :
          condition === "stale" ? "Not current · last conversation retained" :
          condition === "unavailable" ? "Conversation unavailable" : ""));
        mark(scroller, "aria-label", (demo ? "Synthetic Demo conversation with " : "Conversation with ") + (peer || "no seat"));
        mark(scroller, "aria-busy", String(condition === "loading"));

        const more = OLDER.has(model.older) ? model.older : "none";
        hide(older, more !== "more" && more !== "loading");
        say(older, more === "loading" ? "Loading older messages…" : "Older messages");
        mark(older, "aria-disabled", String(more === "loading"));
        hide(olderNote, more !== "truncated");
        say(olderNote, "Older history is not loaded here; it remains in the fleet's own message history.");

        const first = !memo.drawn, arrived = [], changed = [];
        const firstKnown = rows.findIndex(function (r) { return memo.known.has(r.key) || (r.supersedes && memo.known.has(r.supersedes)); });
        rows.forEach(function (row, index) {
          if (row.supersedes && memo.known.has(row.supersedes)) memo.known.add(row.key);
          if (!memo.known.has(row.key)) { memo.known.add(row.key); if (!first && (firstKnown < 0 || index > firstKnown)) arrived.push(row); }
        });
        const following = memo.follow;          // design 42.1: scroll to new activity only while already following
        if (arrived.length && following) memo.firstNew = "";
        else if (arrived.length && !memo.unseen) memo.firstNew = arrived[0].key;

        const wanted = new Set();
        let day = "";
        rows.forEach(function (row, index) {
          wanted.add(row.key);
          let entry = nodes.get(row.key);
          if (!entry && row.supersedes && nodes.has(row.supersedes)) {
            entry = nodes.get(row.supersedes); nodes.delete(row.supersedes); nodes.set(row.key, entry);
            if (memo.open.has(row.supersedes)) memo.open.add(row.key);
            if (memo.firstNew === row.supersedes) memo.firstNew = row.key;
          }
          const born = !entry;
          if (born) { entry = {item: el("li"), sign: "", body: null}; nodes.set(row.key, entry); }
          mark(entry.item, "data-key", row.key);
          const rowDay = dayOf(row.time), newDay = rowDay && rowDay !== day ? rowDay : "";
          if (rowDay) day = rowDay;
          const replies = row.id ? rows.filter(function (r) { return r.replyTo === row.id && !r.owner === row.owner; }) : [];
          const sign = signature(row, replies, newDay, memo.firstNew === row.key);
          if (sign !== entry.sign) {
            if (!born && !first) changed.push(row);
            entry.sign = sign; paint(entry, row, replies, newDay, rows);
          }
          entry.item.classList.toggle("fresh", born && !first && !hush && arrived.indexOf(row) >= 0);
          if (list.children[index] !== entry.item) list.insertBefore(entry.item, list.children[index] || null);
        });
        Array.from(nodes.keys()).forEach(function (k) { if (!wanted.has(k)) { nodes.get(k).item.remove(); nodes.delete(k); } });
        /* What is remembered per thread never outgrows the supplied window. A loading or unavailable render keeps
         * the memory, so the rows it will show next are not mistaken for new ones. */
        if (condition === "ready" || condition === "stale") {
          memo.known = wanted;
          memo.open.forEach(function (k) { if (!wanted.has(k)) memo.open.delete(k); });
          if (memo.firstNew && !wanted.has(memo.firstNew)) memo.firstNew = "";
          if (memo.anchor && !wanted.has(memo.anchor.key)) memo.anchor = null;
        }

        hide(empty, rows.length > 0 || condition === "loading");
        say(empty, !peer ? "Choose a seat to open its conversation." :
          condition === "unavailable" ? "This conversation cannot be shown right now. What you have typed is kept." :
          "No messages with " + peer + " yet. A message you send appears here with its receipts.");

        if (condition !== "loading") memo.drawn = true;
        if (visible()) {
          if (moved) {
            if (memo.follow) toEnd(); else if (!holdAnchor(memo.anchor)) move(memo.top);
          } else if (following) toEnd();
          else if (holdAnchor(anchor)) memo.anchor = anchor;
        }
        if (arrived.length && !following) memo.unseen += arrived.length;
        if (memo.follow && rows.length && visible()) markSeen();
        badge();
        if (changed.length && !hush) {
          const last = changed[changed.length - 1];
          say(spoken, (last.owner ? "Your message " : "Message from " + last.from + " ") + (last.id ? shortId(last.id) : "") + ": " + summary(last));
        }
      } finally { rendering = false; }
      flush();
    }

    function onScroll() {
      if (!memo || rendering || !visible() || !settled()) return;
      if (Math.abs(scroller.scrollTop - echo) < 1) { echo = -1; return; }
      echo = -1; track();
      if (memo.follow) { markSeen(); badge(); flush(); }
    }
    function reveal(key, flash) {
      const entry = nodes.get(key); if (!entry) return;
      const edge = scroller.getBoundingClientRect(), box = entry.item.getBoundingClientRect();
      if (box.top < edge.top || box.bottom > edge.bottom) move(scroller.scrollTop + box.top - edge.top - 12);
      if (flash && entry.item.animate && !document.body.classList.contains("motion-off")) {
        entry.item.animate([{filter: "brightness(1.9)"}, {filter: "brightness(1)"}], {duration: 900, easing: "ease-out"});
      }
    }
    function onClick(event) {
      const control = event.target.closest ? event.target.closest("button") : null;
      if (!control || !root.contains(control) || !memo) return;
      const keyboard = event.detail === 0;
      if (control === fresh) {
        const target = memo.firstNew;
        memo.follow = true; toEnd(); markSeen(); badge();
        if (target) { reveal(target, false); track(); }
        if (keyboard) scroller.focus({preventScroll: true});
        flush(); return;
      }
      if (control === older) { if (older.getAttribute("aria-disabled") !== "true") { call("onOlder", thread); flush(); } return; }
      const item = control.closest("li[data-key]"), key = item && item.dataset.key;
      const role = control.dataset.role;
      if (role === "jump" || role === "origin") { reveal(control.dataset.target, true); track(); return; }
      if (role === "more" && key) {
        const anchor = anchorNow();
        if (memo.open.has(key)) memo.open.delete(key); else memo.open.add(key);
        const open = memo.open.has(key);
        nodes.get(key).body.classList.toggle("clamped", !open);
        control.textContent = open ? "Show less" : "Show all"; control.setAttribute("aria-expanded", String(open));
        holdAnchor(anchor); track();
        return;
      }
      if (role === "reply" && key) {
        const entry = nodes.get(key), id = entry && JSON.parse(entry.sign)[0].id;
        if (id) { call("onReply", thread, id); flush(); }
      }
    }
    /* A pointer press on the thread's own floating control must not take focus from the owner's input. */
    function onPress(event) { if (event.target === fresh) event.preventDefault(); }
    function onResize() {
      if (!visible()) { wasHidden = true; return; }
      const reflowed = wasHidden || scroller.clientWidth !== sized.w;
      sized.w = scroller.clientWidth; sized.h = scroller.clientHeight; wasHidden = false;
      if (!memo || rendering) return;
      if (memo.follow) toEnd();
      else if (!holdAnchor(memo.anchor) && reflowed) move(memo.top);
    }
    scroller.addEventListener("scroll", onScroll, {passive: true});
    root.addEventListener("click", onClick);
    fresh.addEventListener("mousedown", onPress);
    watcher = typeof ResizeObserver === "function" ? new ResizeObserver(onResize) : null;
    if (watcher) { watcher.observe(scroller); watcher.observe(list); }

    return {
      render: render,
      destroy: function () {
        if (!alive) return;
        alive = false;
        if (watcher) watcher.disconnect();
        scroller.removeEventListener("scroll", onScroll); root.removeEventListener("click", onClick);
        fresh.removeEventListener("mousedown", onPress);
        nodes.clear(); threads.clear(); calls.length = 0;
        root.classList.remove("convo-mounted", "demo"); delete root.dataset.state;
        root.replaceChildren();
      }
    };
  }
  global.ConversationView = {mount: mount, version: 1};
}(window));
