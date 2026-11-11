/* Explicit owner Control. Output, onData and mouse events have no writer. */
"use strict";
(function (root) {
  function mount(config) {
    const $ = id => document.getElementById(id), term = config.term, screen = $("terminal-screen");
    const bar = $("control-mode").parentNode, messageNode = $("control-state");
    const home = bar && bar.parentNode, anchor = document.createElement("span");
    anchor.hidden = true; if (home) home.insertBefore(anchor, bar);
    const tab = crypto.randomUUID();
    let phase = "idle", generation = 0, selected = null, nonce = null, lease = null, target = null;
    let abort = null, inputAbort = null, ticker = null, heartbeat = false, until = 0, resizeTimer = null, switched = false, renderToken = null;
    let queue = [], queuedBytes = 0, inflight = false, next = 1, composing = false;
    const committed = new WeakSet();
    function current() { return config.selection(); }
    function key(value) { return value ? [value.fleet, value.seat, value.view].join("|") : null; }
    function same() { return selected && key(current()) === key(selected); }
    function busy() { return phase !== "idle"; }
    function enable(value) {
      term.options.disableStdin = !value;
      const helper = screen.querySelector(".xterm-helper-textarea");
      if (helper) { helper.readOnly = !value; helper.tabIndex = value ? 0 : -1; helper.setAttribute("aria-hidden", String(!value)); }
      screen.setAttribute("aria-label", value ? "Owner-controlled native terminal" : "Read-only ANSI terminal");
    }
    function caption() {
      return target ? target.fleet_label + " / " + target.seat + " · " + target.pane + " · " + target.terminal : "Choose a registered terminal";
    }
    function render(message) {
      const persistent = ["acquiring", "active", "stopping", "incomplete"].includes(phase);
      const expanded = config.expanded && config.expanded();
      // The pane has CSS containment, so a fixed descendant would still scroll
      // with it. Portal these SAME nodes to the body; no duplicated controls.
      if (home && persistent && !expanded && bar.parentNode !== document.body) {
        document.body.append(bar); bar.append(messageNode);
      } else if (home && (!persistent || expanded) && bar.parentNode === document.body) {
        home.insertBefore(bar, anchor); home.insertBefore(messageNode, anchor);
      }
      bar.dataset.phase = phase;
      document.body.classList.toggle("owner-control-present", persistent);
      $("take-control").disabled = !current() || busy() || !config.renderer.available();
      $("release-control").hidden = !persistent;
      $("control-mode").textContent = phase === "active" ? "Control" : "Watch";
      $("control-mode").classList.toggle("controlling", phase === "active");
      $("control-target").textContent = caption();
      const seconds = Math.max(0, Math.ceil((until - Date.now()) / 1000));
      $("control-expiry").textContent = phase === "active" ? "Expires in " + Math.floor(seconds / 60) + ":" + String(seconds % 60).padStart(2, "0") : "";
      if (message) $("control-state").textContent = message;
      if (phase === "active") { $("esc-hint").textContent = "Esc is native · Ctrl+Shift+X releases"; $("esc-hint").hidden = false; }
    }
    function clearInput() {
      enable(false); queue = []; queuedBytes = 0; composing = false;
      if (renderToken) config.renderer.invalidate(renderToken); renderToken = null;
      if (inputAbort) inputAbort.abort(); inputAbort = null;
      clearTimeout(resizeTimer); resizeTimer = null;
      clearInterval(ticker); ticker = null;
      heartbeat = false;
    }
    async function cancelOriginal(original) {
      if (!original) return "released";
      const request = new AbortController(), timeout = setTimeout(function () { request.abort(); }, 2500);
      try {
        const response = await config.api("/api/control/release", original, request.signal);
        const result = await response.json(); return result.state;
      } finally { clearTimeout(timeout); }
    }
    async function release(message) {
      if (!busy() || phase === "stopping") return;
      const old = lease ? {lease: lease, tab: tab} : nonce ? {nonce: nonce, tab: tab} : null;
      const wasController = switched;
      const mine = ++generation;
      clearInput(); if (abort) abort.abort(); abort = null;
      if ($("control-confirmation").open) $("control-confirmation").close();
      phase = "stopping"; render("Releasing Control; accepted input or native resume is not undone.");
      try {
        const result = await cancelOriginal(old);
        if (mine !== generation) return;
        if (result !== "released") throw new Error("Release is incomplete");
        phase = "idle"; nonce = lease = null; inflight = false;
        render(message || "Control released. Output remains disconnected; Watch requires an explicit action.");
        if (wasController) config.status("CONTROL RELEASED · disconnected output from " + caption() + ". Watch requires an explicit action.");
        $("take-control").focus();
      } catch (error) {
        if (mine !== generation) return;
        phase = "incomplete"; render("Release unconfirmed or incomplete. Input disabled; exclusion may remain. Retry Release explicitly.");
      }
    }
    async function prepare() {
      if (busy() || !current() || !config.renderer.available()) return;
      selected = Object.assign({}, current()); target = null; lease = nonce = null; switched = false;
      const mine = ++generation;
      phase = "preparing"; render("Checking this terminal before confirmation…");
      try {
        const response = await config.api("/api/control/prepare", Object.assign({}, selected, {tab: tab}));
        const result = await response.json();
        if (mine !== generation || !same()) {
          cancelOriginal({nonce: result.nonce, tab: tab}).catch(function () {}); return;
        }
        if (!result.nonce || !result.target || !Array.isArray(result.disclosures)) throw new Error("Unsupported confirmation");
        nonce = result.nonce; target = result.target;
        $("control-confirm-target").textContent = caption();
        $("control-disclosures").replaceChildren.apply($("control-disclosures"), result.disclosures.map(function (text) {
          const item = document.createElement("li"); item.textContent = String(text); return item;
        }));
        phase = "confirming"; $("confirm-control").disabled = false;
        $("control-confirmation").showModal(); $("cancel-control").focus(); render("Confirm only the exact terminal shown.");
      } catch (error) {
        if (mine !== generation) return;
        phase = "idle"; render("Control unavailable. " + error.message);
      }
    }
    async function confirm() {
      if (phase !== "confirming" || !same() || !nonce) return;
      if (!config.renderer.available()) { await release("Renderer unavailable; no controller requested."); return; }
      const mine = generation, original = nonce;
      const dims = config.dimensions();
      phase = "acquiring"; $("confirm-control").disabled = true; $("control-confirmation").close();
      render("Acquiring one controller; input remains disabled until its full frame.");
      try {
        const response = await config.api("/api/control/acquire", {nonce: original, tab: tab, confirmed: true, cols: dims.cols, rows: dims.rows});
        const result = await response.json();
        if (mine !== generation || !same()) { cancelOriginal({nonce: original, tab: tab}).catch(function () {}); return; }
        if (!result.lease || result.state !== "ready") throw new Error("Controller was not ready");
        lease = result.lease; target = result.target; next = 1;
        config.stopWatch(); switched = true; renderToken = config.renderer.begin("control");
        abort = new AbortController();
        stream(mine, lease, abort, renderToken).catch(function () {});
      } catch (error) {
        if (mine === generation) await release("Acquisition failed or uncertain. Confirm again explicitly; native resume is not undone.");
      }
    }
    async function stream(mine, originalLease, controller, rendering) {
      let full = false, parsed = false, sequence = null;
      try {
        const response = await config.api("/api/control/stream", {lease: originalLease, tab: tab}, controller.signal);
        const reader = response.body.getReader(), decoder = new TextDecoder(); let pending = "";
        while (true) {
          const item = await reader.read(); if (item.done) break;
          pending += decoder.decode(item.value, {stream: true});
          if (pending.length > 1048576) throw new Error("Control output buffer exceeded");
          let at;
          while ((at = pending.indexOf("\n")) >= 0) {
            const line = pending.slice(0, at); pending = pending.slice(at + 1); if (!line) continue;
            if (mine !== generation || !same() || abort !== controller) return;
            const frame = JSON.parse(line);
            if (frame.type !== "frame" || typeof frame.full !== "boolean" || !Number.isInteger(frame.seq) || frame.seq < 0 ||
                !Number.isInteger(frame.cols) || !Number.isInteger(frame.rows) || frame.cols < 20 || frame.cols > 300 || frame.rows < 5 || frame.rows > 120 ||
                typeof frame.bytes !== "string" || frame.bytes.length > 262144 || (!full && !frame.full) ||
                (sequence !== null && (frame.seq <= sequence || (frame.seq !== sequence + 1 && !frame.full)))) throw new Error("Control stream ended or changed");
            sequence = frame.seq;
            full = true;
            await config.renderer.frame(rendering, frame);
            if (mine !== generation || !same() || abort !== controller || controller.signal.aborted ||
                lease !== originalLease || !config.renderer.ready(rendering)) return;
            if (!parsed && frame.full && phase === "acquiring") {
              parsed = true; phase = "active"; until = Date.now() + 600000;
              enable(true); term.focus(); render("Owner input active; Escape is native. Ctrl+Shift+X releases.");
              config.status("CONTROL · " + caption() + " · native terminal; submission does not establish consumption");
              ticker = setInterval(function () {
                render();
                if (Date.now() >= until) { release("Control expired."); return; }
                if (heartbeat) return;
                heartbeat = true;
                const request = new AbortController(), timeout = setTimeout(function () { request.abort(); }, 2500);
                config.api("/api/control/heartbeat", {lease: originalLease, tab: tab}, request.signal).then(r => r.json()).then(function (result) {
                  if (mine === generation && result.state === "active") until = Math.min(until, Date.now() + result.remaining_seconds * 1000);
                }).catch(function () { if (mine === generation) release("Control heartbeat unavailable; input disabled."); }).finally(function () { clearTimeout(timeout); if (mine === generation) heartbeat = false; });
              }, 2000);
            }
          }
        }
      } catch (error) {
        // Errors are intentionally body-free; no keyboard text enters a notice.
      } finally {
        if (mine === generation) await release("Control stream ended. Input disabled; no automatic reconnect.");
      }
    }
    async function pump() {
      if (inflight || !queue.length || phase !== "active" || !same()) return;
      const job = queue.shift(); queuedBytes -= job.bytes;
      const mine = generation, seq = next++, original = lease; inflight = true;
      const request = new AbortController(); inputAbort = request;
      const timeout = setTimeout(function () { request.abort(); }, 3500);
      try {
        const response = await config.api("/api/control/" + job.kind, Object.assign({lease: original, tab: tab, seq: seq}, job.payload), request.signal);
        const result = await response.json();
        if (mine !== generation) return;
        if (result.status !== "submitted" || result.accepted !== seq) throw new Error("Uncertain submission");
        inflight = false; pump();
      } catch (error) { if (mine === generation) release("Submission uncertain. Control ended; input is never replayed."); }
      finally { clearTimeout(timeout); if (inputAbort === request) inputAbort = null; job.payload = null; }
    }
    function enqueue(kind, payload) {
      if (phase !== "active" || !same()) return;
      if (kind === "input" && typeof payload.text !== "string") return;
      const size = kind === "input" ? new TextEncoder().encode(payload.text).length : 32;
      if ((kind === "input" && (!size || size > 8192)) || queue.length >= 32 || queuedBytes + size > 65536) {
        if (size > 8192) render("Paste/input exceeds 8 KiB including wrappers; nothing from this event was submitted.");
        else release("Input queue full. Control ended; no replay.");
        return;
      }
      queue.push({kind: kind, payload: payload, bytes: size}); queuedBytes += size; pump();
    }
    function focused(event) {
      return phase === "active" && same() && config.renderer.ready(renderToken) && event && event.isTrusted === true &&
        !document.hidden && screen.contains(document.activeElement) && (!event.target || screen.contains(event.target));
    }
    function shortcut(event) { return event.ctrlKey && event.shiftKey && !event.altKey && !event.metaKey && String(event.key).toLowerCase() === "x"; }
    document.addEventListener("keydown", function (event) {
      if (busy() && shortcut(event)) { event.preventDefault(); event.stopImmediatePropagation(); release("Control released with Ctrl+Shift+X."); }
    }, true);
    if (term.onKey) term.onKey(function (item) {
      const event = item.domEvent;
      if (shortcut(event)) return; // Never forward the Release combination, even a synthetic one.
      if (!focused(event) || composing || event.isComposing || event.keyCode === 229) return;
      const key = String(event.key).toLowerCase();
      if (event.metaKey || (event.ctrlKey && (key === "v" || (key === "c" && (event.shiftKey || term.hasSelection()))))) return;
      enqueue("input", {text: item.key});
    });
    screen.addEventListener("paste", function (event) {
      if (!focused(event)) return;
      event.preventDefault(); event.stopPropagation();
      let text = event.clipboardData && event.clipboardData.getData("text/plain"); if (!text) return;
      if (term.modes && term.modes.bracketedPasteMode) text = "\x1b[200~" + text + "\x1b[201~";
      enqueue("input", {text: text});
    }, true);
    screen.addEventListener("compositionstart", function (event) { if (focused(event)) composing = true; }, true);
    screen.addEventListener("compositionend", function (event) {
      const was = composing; composing = false;
      if (!was || !focused(event) || committed.has(event)) return;
      committed.add(event); if (event.data) enqueue("input", {text: event.data});
    }, true);
    $("take-control").addEventListener("click", function (event) { if (event.isTrusted) prepare(); });
    $("confirm-control").addEventListener("click", function (event) { if (event.isTrusted) confirm(); });
    $("cancel-control").addEventListener("click", function () { release("Control cancelled; existing Watch preserved."); });
    $("control-confirmation").addEventListener("cancel", function (event) { event.preventDefault(); release("Control cancelled; existing Watch preserved."); });
    $("release-control").addEventListener("click", function () { release(); });
    function observe() {
      if (["preparing", "confirming", "acquiring", "active"].includes(phase) && (!same() || !current())) release("Selection, view or owner authority changed; input disabled.");
      render(phase === "idle" && !config.renderer.available() ? "Terminal renderer unavailable while its prior parse drains. Input disabled; reopen the launch address if it remains stalled." : null);
    }
    function layout(event) {
      if (!event || event.isTrusted !== true) return;
      render(); // Focused presentation keeps the SAME authority nodes in its flow.
      if (phase !== "active") return;
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(function () {
        if (phase !== "active" || !same()) return;
        const dims = config.dimensions(); enqueue("resize", {cols: dims.cols, rows: dims.rows});
      }, 120);
    }
    enable(false); render("Watch is read only. Take control requires explicit confirmation.");
    return {observe: observe, release: release, busy: busy, layout: layout};
  }
  root.TerminalControl = Object.freeze({mount: mount});
}(window));
