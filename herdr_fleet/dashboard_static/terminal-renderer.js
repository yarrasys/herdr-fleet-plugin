/* One public xterm parse at a time. Reset never cancels queued vendor writes. */
"use strict";
(function (root) {
  function mount(config) {
    const term = config.term, screen = config.screen, copy = config.copy, states = new WeakMap();
    let current = null, pending = null, serial = 0, frames = 0, bytes = 0, fitting = false;
    function fence() {
      screen.classList.toggle("render-fenced", true); screen.setAttribute("aria-hidden", "true");
      screen.inert = true; copy.disabled = true;
    }
    function owns(token) { const state = token && states.get(token); return !!(token === current && state && state.valid); }
    function ready(token) {
      token = token || current; const state = token && states.get(token);
      return !!(owns(token) && state.presented);
    }
    function invalidate(token) {
      if (token && token !== current) return;
      const old = current, state = old && states.get(old);
      current = null;
      if (state) { state.valid = false; state.presented = false; state.abort.abort(); }
      fence();
      const draining = pending;
      if (!draining) term.reset();
      else if (!draining.clearScheduled) {
        draining.clearScheduled = true;
        draining.done.then(function () { if (!current && !pending) term.reset(); });
      }
    }
    function begin(owner) {
      if (owner !== "watch" && owner !== "control") throw new Error("Unknown render owner");
      invalidate();
      const token = Object.freeze({owner: owner, generation: ++serial});
      states.set(token, {valid: true, presented: false, full: false, seq: null, abort: new AbortController()});
      current = token; return token;
    }
    function wait(done, token) {
      const state = token && states.get(token);
      return new Promise(function (resolve, reject) {
        let finished = false;
        function end(error) {
          if (finished) return; finished = true; clearTimeout(timer);
          if (state) state.abort.signal.removeEventListener("abort", cancelled);
          if (error) reject(error); else resolve();
        }
        function cancelled() { end(new Error("Terminal render ownership changed")); }
        const timer = setTimeout(function () {
          if (pending && pending.done === done) pending.stalled = true;
          end(new Error("Terminal renderer stalled; reconnect explicitly"));
        }, 2000);
        if (state) {
          if (!state.valid || state.abort.signal.aborted) { cancelled(); return; }
          state.abort.signal.addEventListener("abort", cancelled, {once: true});
        }
        done.then(function () { end(); }, function () { end(new Error("Terminal parse unavailable")); });
      });
    }
    async function frame(token, value) {
      if (!owns(token)) throw new Error("Terminal render ownership changed");
      let size = 0, reserved = false;
      try {
        if (!value || value.type !== "frame" || typeof value.full !== "boolean" || !Number.isSafeInteger(value.seq) || value.seq < 0 ||
            !Number.isInteger(value.cols) || !Number.isInteger(value.rows) || value.cols < 1 || value.cols > 300 || value.rows < 1 || value.rows > 120 ||
            typeof value.bytes !== "string" || value.bytes.length > 262144) throw new Error("Invalid terminal frame");
        const raw = atob(value.bytes); size = raw.length;
        if (size > 196608 || frames >= 32 || bytes + size > 1048576) throw new Error("Terminal output limit exceeded");
        frames++; bytes += size; reserved = true;
        while (pending) {
          if (pending.stalled) throw new Error("Terminal renderer is unavailable while its prior parse remains pending");
          await wait(pending.done, token);
        }
        if (!owns(token)) throw new Error("Terminal render ownership changed");
        const state = states.get(token);
        if ((!state.full && !value.full) || (state.seq !== null && (value.seq <= state.seq ||
              (value.seq !== state.seq + 1 && !value.full)))) throw new Error("Terminal sequence changed");
        state.seq = value.seq; state.full = true;
        if (value.full) { if (!state.presented) fence(); term.reset(); }
        if (term.cols !== value.cols || term.rows !== value.rows) term.resize(value.cols, value.rows);
        if (size) {
          const data = new Uint8Array(size); for (let n = 0; n < size; n++) data[n] = raw.charCodeAt(n);
          let finish;
          const item = {done: new Promise(function (resolve) { finish = resolve; })};
          pending = item;
          try { term.write(data, function () { if (pending === item) pending = null; finish(); }); }
          catch (error) { if (pending === item) pending = null; finish(); throw error; }
          await wait(item.done, token);
        }
        if (!owns(token)) throw new Error("Terminal render ownership changed");
        state.presented = true; screen.classList.toggle("render-fenced", false);
        screen.setAttribute("aria-hidden", "false"); screen.inert = false; copy.disabled = false;
      } catch (error) {
        if (owns(token)) invalidate(token);
        throw error;
      } finally { if (reserved) { frames--; bytes -= size; } }
    }
    async function localFit(callback) {
      if (fitting || (pending && pending.stalled)) return;
      fitting = true;
      try {
        const original = current;
        while (pending) {
          if (pending.stalled) return;
          await wait(pending.done, null);
        }
        if (original === current) callback();
      } finally { fitting = false; }
    }
    fence();
    return Object.freeze({begin: begin, invalidate: invalidate, frame: frame, ready: ready, owns: owns, localFit: localFit,
      available: function () { return !pending || !pending.stalled; }});
  }
  root.TerminalRenderer = Object.freeze({mount: mount});
}(window));
