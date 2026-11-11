"""Two authenticated dashboard OS processes, one fake native endpoint and real gates."""
from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time

from herdr_fleet import inbox, permits
from test_inbox import box, log_entries
import test_section41 as dashboard_tests


SERVER = r'''
import fcntl,json,os,signal,subprocess,sys,threading
from pathlib import Path
from herdr_fleet import permits
from herdr_fleet.herdr import Herdr
from herdr_fleet.dashboard import DashboardServer
from herdr_fleet.dashboard_data import Registry,Observer
from herdr_fleet.dashboard_actions import OwnerActions
from herdr_fleet.dashboard_terminal import Watches
from herdr_fleet.seats import Clock
import herdr_fleet.dashboard_control as controls
import fake_herdr
world,log,fleet,gate,api=sys.argv[1:]
original=permits.Store.__init__
def isolated(self,root=None): original(self,root if root is not None else gate)
permits.Store.__init__=isolated
controls.PROBE_S=60
env=dict(os.environ,HERDR_SOCKET_PATH=api)
fake=fake_herdr.Fake(world,log,fleet=fleet)
def runner(argv):
 with open(world+'.native-mutex','a') as lock:
  fcntl.flock(lock,fcntl.LOCK_EX)
  return fake(argv)
owned=[]
frame=json.dumps({'type':'terminal.frame','encoding':'ansi','seq':1,'full':True,'width':80,'height':24,'bytes':'U3ludGhldGljIGZyYW1l'})
def spawn(argv,**kwargs):
 evidence=dict(permits.native_transport()[2]);evidence['live_fds']=all(os.fstat(fd).st_ino>0 for fd in kwargs['pass_fds'])
 child=subprocess.Popen([sys.executable,'-u','-c',"import sys,time;print(sys.argv[1],flush=True);sys.stdin.read();time.sleep(60)",frame],**kwargs)
 owned.append(child)
 with server.controls.lock:
  authenticated=any(server.controls.authorized(w.session) and w.state=='acquiring' for w in server.controls.leases.values())
 with open(log,'a') as handle:
  handle.write(json.dumps({'command':['dashboard'],'dashboard_action':'control','dashboard_target':argv[4]})+'\n')
  handle.write(json.dumps({'argv':argv[1:],'exit':0,'info':{'control_permit':evidence,'authenticated_human':authenticated,'native_child':child.pid,'dashboard_pid':os.getpid()}})+'\n')
 return child
native=Herdr('/fake/herdr',runner,stream_runner=spawn);native.configure(env)
registry=Registry([fleet],str(Path(fleet).parent.parent),env)
observer=Observer(native,registry,env);initial=observer.refresh()
actions=OwnerActions(native,registry,env,Clock())
server=DashboardServer(('127.0.0.1',0),observer,Watches(native,registry),actions)
ident=next(iter(registry.paths));seats=observer.snapshot()['fleets'][0]['seats']
if not seats: raise RuntimeError('synthetic fleet observation unavailable: '+str(initial))
seat=next(s for s in seats if s['name']=='sample-dev')
signal.signal(signal.SIGTERM,lambda *a:threading.Thread(target=server.shutdown,daemon=True).start())
print(json.dumps({'port':server.server_address[1],'capability':server.authority.launch,'fleet':ident,'view':seat['view']}),flush=True)
try: server.serve_forever(poll_interval=0.05)
finally:
 server.server_close()
 if any(p.poll() is None for p in owned): raise SystemExit(3)
'''


class IndependentDashboards(dashboard_tests.DashboardCase):
    def start(self, fleet, api):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=os.pathsep.join(
            [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]))
        process = subprocess.Popen([sys.executable, "-u", "-c", SERVER, self.world_path, self.log_path,
                                    fleet, os.path.join(self.tmp, "permits"), api],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self.services.append(process)
        self.assertTrue(select.select([process.stdout], [], [], 5)[0])
        line = process.stdout.readline()
        self.assertTrue(line, process.stderr.read() if process.poll() is not None else "dashboard startup stalled")
        data = json.loads(line); data["process"] = process
        code, headers, raw = self.post(data, "/api/session", {"capability": data["capability"]}, auth=False)
        self.assertEqual(code, 200, raw)
        data["cookie"] = headers["Set-Cookie"].split(";", 1)[0]; data["csrf"] = json.loads(raw)["csrf"]
        return data

    def post(self, service, route, body, auth=True):
        connection = http.client.HTTPConnection("127.0.0.1", service["port"], timeout=5)
        origin = "http://127.0.0.1:%d" % service["port"]
        headers = {"Host": "127.0.0.1:%d" % service["port"], "Origin": origin, "Content-Type": "application/json"}
        if auth: headers.update(Cookie=service["cookie"], **{"X-Fleet-CSRF": service["csrf"]})
        connection.request("POST", route, json.dumps(body), headers)
        response = connection.getresponse()
        if route.endswith("/stream"):
            self.streams.append((connection, response))
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.readline())["type"], "frame")
            return
        try: return response.status, dict(response.getheaders()), response.read()
        finally: connection.close()

    def prepare(self, service):
        body = dict(fleet=service["fleet"], seat="sample-dev", view=service["view"], tab="synthetic_two_dashboard_tab")
        code, _, raw = self.post(service, "/api/control/prepare", body)
        self.assertEqual(code, 200, raw)
        return json.loads(raw)["nonce"]

    def acquire(self, service, expected):
        body = dict(nonce=self.prepare(service), tab="synthetic_two_dashboard_tab", confirmed=True, cols=80, rows=24)
        code, _, raw = self.post(service, "/api/control/acquire", body)
        self.assertEqual(code, expected, raw)
        if code == 200: return dict(lease=json.loads(raw)["lease"], tab=body["tab"])

    def test_authenticated_processes_contend_across_roots_and_orphan_keeps_routed_delivery_held(self):
        self.services, self.streams = [], []
        orphan = None
        self.write_fleet('watch=false\n' + self.read_fleet()); self.sync()
        partner = os.path.join(self.tmp, "partner")
        os.makedirs(os.path.join(partner, ".herdr"))
        second_file = os.path.join(partner, ".herdr/fleet.toml")
        Path(second_file).write_text('workspace="sample-partner"\nwatch=false\n[[agents]]\nname="sample-dev"\nkind="claude"\n')
        source = os.path.join(self.tmp, "source")
        os.makedirs(os.path.join(source, ".herdr"))
        source_file = os.path.join(source, ".herdr/fleet.toml")
        Path(source_file).write_text('workspace="sample-source"\nwatch=false\n[[agents]]\nname="sample-source-lead"\nkind="claude"\n[[contacts]]\nname="held-peer"\nmatch={name="sample-dev"}\n')
        api = os.path.join(self.tmp, "synthetic-native-api.sock")
        self.mark("control", "term_w2:p2")
        try:
            first = self.start(os.path.join(self.repo, ".herdr/fleet.toml"), api)
            second = self.start(second_file, api)
            one = self.acquire(first, 200); self.post(first, "/api/control/stream", one)
            self.acquire(second, 400)
            events = [c for c in self.calls() if c["argv"][:3] == ["terminal", "session", "control"]]
            self.assertEqual(len(events), 1)
            orphan = events[0]["info"]["native_child"]
            first["process"].kill(); first["process"].wait(timeout=2)
            env = dict(self.env, HERDR_SOCKET_PATH=api)
            code, out, err = self.run_cli("send", "held-peer", "Synthetic cross-root hold", "--now", "--as", "owner", "--fleet", source_file, env=env)
            self.assertEqual(code, 0, out + err); self.assertIn("owner control", out)
            ident, = box(self.repo, "sample-dev", inbox.NEW)
            self.assertFalse(self.prompts_to("sample-dev"))
            self.acquire(second, 400)
            self.assertEqual(len(self.argvs("terminal", "session", "control")), 1)
            os.kill(orphan, signal.SIGTERM); orphan = None
            domain = permits.endpoints(env)[0]; end = time.monotonic() + 2
            while True:
                try:
                    with permits.Store().acquire([domain], [("N", "sample-dev")]): break
                except permits.Held:
                    if time.monotonic() >= end: raise
                    time.sleep(0.01)
            two = self.acquire(second, 200); self.post(second, "/api/control/stream", two)
            self.assertEqual(len(self.argvs("terminal", "session", "control")), 2)
            code, _, raw = self.post(second, "/api/control/release", two)
            self.assertEqual(code, 200); self.assertEqual(json.loads(raw)["state"], "released")
            self.assertEqual(self.run_cli("deliver", "sample-dev", env=env)[0], 0)
            self.assertEqual(box(self.repo, "sample-dev", inbox.NEW), [])
            self.assertEqual(box(self.repo, "sample-dev", inbox.CUR), [ident])
            self.assertEqual(len(self.prompts_to("sample-dev")), 1)
            for root in (self.repo, source):
                entries = [e for e in log_entries(root) if e.get("id") == ident]
                self.assertEqual(len([e for e in entries if e["kind"] == "delivered"]), 1)
                self.assertFalse(any(e["kind"] in ("refused", "dropped") for e in entries))
        finally:
            for connection, response in self.streams: response.close(); connection.close()
            for process in self.services:
                if process.poll() is None: process.terminate()
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=2)
                process.stdout.close(); process.stderr.close()
            if orphan is not None:
                try: os.kill(orphan, signal.SIGTERM)
                except ProcessLookupError: pass
