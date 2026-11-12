"""Foreground loopback dashboard. Authentication precedes every private API."""
from __future__ import annotations

import hmac
import json
import os
import secrets
import socket
import threading
import time
import webbrowser
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

from herdr_fleet.context import preflight, UsageError
from herdr_fleet.dashboard_data import Registry, Observer, DataError
from herdr_fleet.dashboard_actions import OwnerActions, Conflict
from herdr_fleet.dashboard_terminal import Watches
from herdr_fleet.dashboard_demo import Demo
from herdr_fleet.dashboard_control import Controls, WatcherUpgradeRequired, WATCHER_RESTART

ASSET_ROOT = os.path.join(os.path.dirname(__file__), "dashboard_static")
ASSETS = {"/": ("index.html", "text/html; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/conversation-view.js": ("conversation-view.js", "text/javascript; charset=utf-8"),
          "/attention-view.js": ("attention-view.js", "text/javascript; charset=utf-8"),
          "/terminal-control.js": ("terminal-control.js", "text/javascript; charset=utf-8"),
          "/terminal-renderer.js": ("terminal-renderer.js", "text/javascript; charset=utf-8"),
          "/dashboard.css": ("dashboard.css", "text/css; charset=utf-8"),
          "/vendor/xterm.js": ("vendor/xterm.js", "text/javascript; charset=utf-8"),
          "/vendor/addon-fit.js": ("vendor/addon-fit.js", "text/javascript; charset=utf-8"),
          "/vendor/xterm.css": ("vendor/xterm.css", "text/css; charset=utf-8")}
MAX_BODY = 49152
CONTROL_ROUTES = {"/api/control/" + name for name in
                  ("prepare", "acquire", "stream", "input", "resize", "heartbeat", "release")}


class Authority:
    def __init__(self):
        self.launch = secrets.token_urlsafe(32)
        self.lock = threading.Lock()
        self.sessions = {}
        self.cookie_name = "fleet_" + secrets.token_hex(8)

    def exchange(self, capability, cookie=None):
        if (not isinstance(capability, str) or len(capability) > 128
                or not hmac.compare_digest(capability.encode("utf-8"), self.launch.encode("ascii"))):
            raise PermissionError("Invalid launch capability; use this process's printed launch address")
        with self.lock:
            now = time.monotonic()
            self.sessions = {k: v for k, v in self.sessions.items() if v[1] > now}
            # A launch is one owner capability. Converge even when two first
            # exchanges arrive without cookies, before either response lands.
            # Recovery is permitted only after the full capability check above.
            if self.sessions:
                token = next(iter(self.sessions))
                return token, self.sessions[token][0]
            token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            self.sessions[token] = (csrf, now + 43200)
        return token, csrf

    def authenticate(self, cookie, csrf):
        try:
            jar = cookies.SimpleCookie()
            jar.load(cookie or "")
            token = jar[self.cookie_name].value
        except (KeyError, cookies.CookieError):
            raise PermissionError("Owner session required; reopen the launch address")
        with self.lock:
            session = self.sessions.get(token)
        if (session is None or session[1] <= time.monotonic() or not isinstance(csrf, str)
                or len(csrf) > 128 or not hmac.compare_digest(session[0].encode("ascii"), csrf.encode("utf-8"))):
            raise PermissionError("Owner session/anti-forgery check failed; reopen the launch address")
        return token


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = False
    block_on_close = True
    allow_reuse_address = False
    request_queue_size = 16

    def __init__(self, address, observer, watches, actions=None, demo=False):
        if address[0] != "127.0.0.1" or type(address[1]) is not int or not 0 <= address[1] <= 65535:
            raise ValueError("dashboard binds only 127.0.0.1 on a valid port")
        self.observer, self.watches, self.actions, self.demo = observer, watches, actions, demo
        self.authority = Authority()
        self.controls = (Controls(watches.herdr, watches.registry, self.authority)
                         if not demo and actions is not None and hasattr(watches, "herdr") else None)
        self.stop_event = threading.Event()
        self.connections = threading.BoundedSemaphore(24)
        self.worker = None
        super().__init__(address, Handler)
        self.origin = "http://127.0.0.1:%d" % self.server_address[1]
        self.host = "127.0.0.1:%d" % self.server_address[1]

    @property
    def launch_url(self):
        return self.origin + "/#" + self.authority.launch

    def process_request(self, request, client_address):
        if not self.connections.acquire(False):
            request.close()
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.connections.release()

    def handle_error(self, request, client_address):
        # No request/secret/body or raw engine exception goes to a traceback/log.
        pass

    def start_observation(self):
        if self.demo:
            return
        self.observer.refresh()
        def refresh():
            while not self.stop_event.wait(2.0):
                self.observer.refresh()
        self.worker = threading.Thread(target=refresh, name="dashboard-observation")
        self.worker.start()

    def server_close(self):
        self.stop_event.set()
        if self.controls is not None:
            self.controls.close()
        self.watches.close()
        if self.worker:
            self.worker.join()
        super().server_close()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "FleetDashboard"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, format, *args):
        pass

    def headers_safe(self, style_nonce=None):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        style = "'self'" + (" 'nonce-%s'" % style_nonce if style_nonce else "")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src %s; "
                         "connect-src 'self'; img-src 'self'; font-src 'self'; object-src 'none'; "
                         "base-uri 'none'; frame-ancestors 'none'; form-action 'none'" % style)

    def respond(self, code, value, mime="application/json; charset=utf-8", cookie=None):
        body = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode("utf-8")
        if len(body) > 4194304:
            code, body, mime = 413, b'{"error":"Response exceeds the 4 MiB dashboard limit; request a smaller page"}', "application/json"
        nonce = secrets.token_urlsafe(24) if mime.startswith("text/html") else None
        if nonce:
            body = body.replace(b"__FLEET_STYLE_NONCE__", nonce.encode("ascii"))
        self.send_response(code)
        self.headers_safe(nonce)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.close_connection = True
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def validate_host(self):
        if (len(self.headers.get_all("Host", [])) != 1 or self.headers.get("Host") != self.server.host
                or len(self.headers.get_all("Origin", [])) > 1
                or len(self.headers.get_all("Sec-Fetch-Site", [])) > 1
                or self.headers.get("Sec-Fetch-Site") not in (None, "same-origin", "none")):
            raise PermissionError("Use the exact loopback launch address")
        if self.headers.get("Origin") not in (None, self.server.origin):
            raise PermissionError("Foreign Origin is not permitted")

    def authenticate(self, mutation=False):
        self.validate_host()
        if mutation and self.headers.get("Origin") != self.server.origin:
            raise PermissionError("Same-origin JSON requests require the exact Origin")
        if len(self.headers.get_all("Cookie", [])) != 1 or len(self.headers.get_all("X-Fleet-CSRF", [])) != 1:
            raise PermissionError("Exactly one owner session and anti-forgery header are required")
        return self.server.authority.authenticate(self.headers.get("Cookie"), self.headers.get("X-Fleet-CSRF"))

    def body(self, maximum=MAX_BODY, strict=False):
        if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) != 1:
            raise DataError("A bounded Content-Length is required")
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            raise DataError("Use application/json")
        try:
            size = int(self.headers["Content-Length"])
        except (ValueError, TypeError):
            raise DataError("Invalid Content-Length")
        if not 0 < size <= maximum:
            raise DataError("Request body exceeds %d bytes or is empty" % maximum)
        raw = self.rfile.read(size)
        if len(raw) != size:
            raise DataError("Incomplete request body")
        try:
            def pairs(items):
                result = {}
                for key, value in items:
                    if key in result: raise ValueError("duplicate JSON field")
                    result[key] = value
                return result
            value = json.loads(raw, object_pairs_hook=pairs) if strict else json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise DataError("Malformed JSON")
        if not isinstance(value, dict):
            raise DataError("JSON must be an object")
        return value

    def do_GET(self):
        try:
            self.validate_host()
            parsed = urlsplit(self.path)
            if parsed.path == "/favicon.ico" and not parsed.query:
                self.respond(204, b"")
                return
            if parsed.path in ASSETS and not parsed.query:
                relative, mime = ASSETS[parsed.path]
                path = os.path.join(ASSET_ROOT, relative)
                if os.path.realpath(path) != os.path.abspath(path):
                    raise DataError("Asset is unavailable")
                with open(path, "rb") as handle:
                    body = handle.read(2097153)
                if len(body) > 2097152:
                    raise DataError("Asset exceeds the size limit")
                self.respond(200, body, mime)
                return
            self.authenticate()
            if parsed.path == "/api/snapshot" and not parsed.query:
                self.respond(200, self.server.observer.snapshot())
            elif parsed.path == "/api/attention":
                query = parse_qs(parsed.query, strict_parsing=True, max_num_fields=3, keep_blank_values=True)
                if (set(query) - {"scope", "fleet", "category"} or any(len(v) != 1 for v in query.values())):
                    raise DataError("Attention accepts only scope, registered fleet and category")
                self.respond(200, self.server.observer.attention_page(query.get("scope", ["all"])[0],
                             query.get("fleet", [None])[0], query.get("category", ["all"])[0]))
            elif parsed.path == "/api/conversation":
                query = parse_qs(parsed.query, strict_parsing=True, max_num_fields=5)
                if (set(query) - {"source", "fleet", "seat", "limit"}
                        or not {"source", "fleet", "seat"}.issubset(query)
                        or any(len(values) != 1 for values in query.values())):
                    raise DataError("Conversation accepts only a registered fleet pair, canonical seat and bounded limit")
                self.respond(200, self.server.observer.conversation(query["source"][0], query["fleet"][0],
                             query["seat"][0], int(query.get("limit", ["50"])[0])))
            elif parsed.path == "/api/list":
                query = parse_qs(parsed.query, strict_parsing=True, max_num_fields=6)
                if (set(query) - {"fleet", "mode", "name", "limit", "id"}
                        or any(len(v) != 1 for v in query.values())):
                    raise DataError("Invalid listing parameters")
                mode = query.get("mode", ["messages"])[0]
                if mode not in ("messages", "log", "watch-log"):
                    raise DataError("Unknown listing mode")
                result = self.server.observer.listing(query.get("fleet", [""])[0], mode,
                                                     query.get("name", ["owner"])[0],
                                                     int(query.get("limit", ["50"])[0]), query.get("id", [None])[0])
                self.respond(200, result)
            else:
                self.respond(404, {"error": "Unknown dashboard endpoint"})
        except PermissionError as exc:
            self.respond(403, {"error": str(exc)[:240]})
        except (DataError, ValueError, OSError):
            self.respond(400, {"error": "Data unavailable or invalid request; refresh the dashboard"})

    def do_POST(self):
        try:
            self.validate_host()
            if self.headers.get("Origin") != self.server.origin:
                raise PermissionError("Same-origin JSON requests require the exact Origin")
            path = urlsplit(self.path)
            if path.query:
                raise DataError("Query strings are not accepted for actions")
            if path.path == "/api/session":
                body = self.body()
                if set(body) != {"capability"}:
                    raise DataError("Session exchange accepts only the launch capability")
                token, csrf = self.server.authority.exchange(body["capability"], self.headers.get("Cookie"))
                self.respond(200, {"csrf": csrf, "demo": self.server.demo},
                             cookie="%s=%s; HttpOnly; SameSite=Strict; Path=/" % (self.server.authority.cookie_name, token))
                return
            session = self.authenticate(mutation=True)
            control = path.path in CONTROL_ROUTES
            body = self.body(65536 if path.path == "/api/control/input" else MAX_BODY, strict=control)
            if path.path == "/api/action":
                if self.server.demo or self.server.actions is None:
                    self.respond(403, {"error": "Demo has no live owner actions"})
                    return
                self.respond(200, self.server.actions.perform(body))
            elif control:
                if self.server.controls is None or self.server.demo:
                    self.respond(403, {"error": "Demo has no native Control"})
                    return
                if path.fragment or path.scheme or path.netloc:
                    raise DataError("Control accepts no URL authority")
                action = path.path.rsplit("/", 1)[1]
                if action == "stream":
                    self.server.controls.schema(action, body)
                    self.stream(body, session, control=True)
                else:
                    result = self.server.controls.perform(action, body, session)
                    try:
                        self.respond(200, result)
                    except OSError:
                        self.close_connection = True
                        if action in ("input", "resize"):
                            self.server.controls.uncertain(body, session)
            elif path.path == "/api/watch":
                if set(body) != {"fleet", "seat", "view", "cols", "rows"}:
                    raise DataError("Watch accepts only a registered seat view and viewport")
                if (not all(isinstance(body[k], str) for k in ("fleet", "seat", "view"))
                        or type(body["cols"]) is not int or type(body["rows"]) is not int
                        or not 20 <= body["cols"] <= 300 or not 5 <= body["rows"] <= 120):
                    raise DataError("Invalid Watch target/viewport")
                self.stream(body, session)
            elif path.path == "/api/demo/replay" and self.server.demo and not body:
                self.server.observer.replay()
                self.respond(200, {"demo": True})
            elif path.path == "/api/demo/reply" and self.server.demo and not body:
                self.server.observer.reply()
                self.respond(200, {"demo": True})
            else:
                self.respond(404, {"error": "No terminal input, lifecycle or arbitrary-command endpoint is available"})
        except PermissionError as exc:
            self.respond(403, {"error": str(exc)[:240]})
        except Conflict as exc:
            self.respond(409, {"error": str(exc)[:240]})
        except WatcherUpgradeRequired:
            self.respond(400, {"error": WATCHER_RESTART})
        except (DataError, ValueError, OSError):
            self.respond(400, {"error": "Action invalid or target changed; refresh before a deliberate new attempt"})

    def stream(self, body, session, control=False):
        self.send_response(200)
        self.headers_safe()
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        def disconnected():
            if self.server.stop_event.is_set():
                return True
            with self.server.authority.lock:
                entry = self.server.authority.sessions.get(session)
                if entry is None or entry[1] <= time.monotonic():
                    return True
            try:
                import select
                if select.select([self.connection], [], [], 0)[0]:
                    return self.connection.recv(1, socket.MSG_PEEK) == b""
            except OSError:
                return True
            return False
        iterator = (self.server.controls.frames(body, session, disconnected) if control else
                    self.server.watches.frames(body["fleet"], body["seat"], body["view"],
                                              body["cols"], body["rows"], disconnected))
        try:
            for frame in iterator:
                self.wfile.write(json.dumps(frame).encode() + b"\n")
                self.wfile.flush()
                if disconnected():
                    break
        except (DataError, ValueError):
            try:
                message = ("Control ended; input is disabled. Confirm a new attempt explicitly." if control else
                           "Watch target changed or frame invalid; refresh and reconnect")
                self.wfile.write(json.dumps({"type": "error", "message": message}).encode() + b"\n")
                self.wfile.flush()
            except OSError:
                pass
        except OSError:
            pass
        finally:
            iterator.close()

    def do_OPTIONS(self):
        self.respond(405, {"error": "Cross-origin access is unavailable"})

    def do_HEAD(self):
        self.respond(405, b"")

    do_PUT = do_OPTIONS
    do_DELETE = do_OPTIONS
    do_PATCH = do_OPTIONS


def serve(ctx, args):
    if not 0 <= args.port <= 65535:
        raise UsageError("--port must be between 0 and 65535")
    if args.demo:
        if args.fleets or args.all_fleets or args.context:
            raise UsageError("--demo does not register live fleets or read live context")
        observer = watches = Demo()
        actions = None
    else:
        ctx.herdr = ctx.herdr.bounded_dashboard_client()
        preflight(ctx, caller=False)
        registry = Registry(args.fleets, ctx.cwd, ctx.env, args.all_fleets)
        observer = Observer(ctx.herdr, registry, ctx.env, args.context)
        watches = Watches(ctx.herdr, registry)
        actions = OwnerActions(ctx.herdr, registry, ctx.env, ctx.clock)
    server = DashboardServer(("127.0.0.1", args.port), observer, watches, actions, args.demo)
    try:
        server.start_observation()
        ctx.say("Fleet dashboard %s: %s" % ("DEMO — synthetic only" if args.demo else "LIVE — owner session", server.launch_url))
        ctx.stdout.flush()
        if args.open_browser:
            webbrowser.open(server.launch_url)
        try:
            server.serve_forever(poll_interval=0.25)
        except KeyboardInterrupt:
            pass
    finally:
        server.server_close()
    return 0
