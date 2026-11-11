"""Cooperative operation and dedicated human-control permits (45.3).

Locks deliberately keep their inodes forever. Closing an owner descriptor, rather
than LOCK_UN, preserves exclusion while an exact native child inherits that open
file description. Nothing here discovers fleets or accepts browser capabilities.
"""
from __future__ import annotations

import contextlib
import contextvars
import errno
import fcntl
import hashlib
import os
import re
import resource
import stat
import threading
from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

API = "HERDR_SOCKET_PATH"
CLIENT = "HERDR_CLIENT_SOCKET_PATH"
ORDER = {"W": 0, "N": 1, "P": 2, "T": 3}
_SEAL = object()
_TRANSPORT = contextvars.ContextVar("fleet_native_transport", default=None)


@contextlib.contextmanager
def transport(env, descriptors=(), evidence=None):
    """Transport context only: this does not create or stand in for a permit."""
    token = _TRANSPORT.set((dict(env), tuple(descriptors), evidence))
    try:
        yield
    finally:
        _TRANSPORT.reset(token)


def native_transport():
    return _TRANSPORT.get()


def default_root() -> str:
    return os.path.join(os.path.realpath("/tmp"), "herdr-fleet-control-%d" % os.geteuid())


class Unavailable(Exception):
    """Cannot establish cooperation; no affected submission is permitted."""


class Held(Unavailable):
    """A cooperating owner/operation currently holds the required resource."""


def absolute(value: object) -> str:
    if (not isinstance(value, str) or not value or not os.path.isabs(value)
            or "\0" in value or len(value.encode("utf-8")) > 4096):
        raise Unavailable("unavailable endpoint: a nonempty absolute path is required")
    return value


def client_path(api: str) -> str:
    # Rust Path::file_stem: a leading dot is not an extension separator.
    parent, filename = os.path.split(absolute(api))
    if filename in ("", ".", ".."):
        raise Unavailable("unavailable endpoint filename")
    split = filename.rfind(".")
    stem = filename[:split] if split > 0 else filename
    return os.path.join(parent, stem + "-client.sock")


@dataclass(frozen=True)
class Domain:
    api: str
    original: str
    client: str
    client_matches: bool

    @property
    def key(self) -> str:
        return hashlib.sha256(self.api.encode("utf-8")).hexdigest()

    def frozen(self, original_env: Mapping[str, str], client_protocol: bool = False) -> Dict[str, str]:
        if client_protocol and not self.client_matches:
            raise Unavailable("unavailable endpoint: API/client aliases or legacy override disagree")
        env = dict(original_env)
        env[API] = self.api
        env.pop(CLIENT, None)
        return env


def endpoints(env: Mapping[str, str]) -> List[Domain]:
    """Pure conservative release/debug candidates, including absent ancestors."""
    if API in env:
        paths = [absolute(env[API])]
    else:
        base = absolute(env["XDG_CONFIG_HOME"]) if "XDG_CONFIG_HOME" in env else os.path.join(absolute(env.get("HOME")), ".config")
        session = env.get("HERDR_SESSION")
        if session is not None and (not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", session)
                                    or session in (".", "..")):
            raise Unavailable("unavailable endpoint: invalid session")
        suffix = ["sessions", session] if session and session != "default" else []
        paths = [os.path.join(base, profile, *suffix, "herdr.sock") for profile in ("herdr", "herdr-dev")]
    result = {}
    for original in paths:
        canonical = os.path.realpath(original)
        client = os.path.realpath(client_path(canonical))
        matches = os.path.realpath(client_path(original)) == client
        if API not in env and CLIENT in env:
            try:
                matches = matches and os.path.realpath(absolute(env[CLIENT])) == client
            except Unavailable:
                matches = False
        result[canonical] = Domain(canonical, original, client, matches)
    return [result[key] for key in sorted(result)]


def selected(candidates: Sequence[Domain], status: object) -> Domain:
    """Consume the actual top-level status-server JSON, never the RPC envelope."""
    if (not isinstance(status, dict) or type(status.get("running")) is not bool
            or (status.get("status"), status["running"]) not in (("running", True), ("not_running", False))):
        raise Unavailable("unavailable endpoint: malformed native server status")
    canonical = os.path.realpath(absolute(status.get("socket")))
    found = [d for d in candidates if d.api == canonical]
    if len(found) != 1:
        raise Unavailable("unavailable endpoint: selected binary disagrees with endpoint candidates")
    return found[0]


def resources(values: Iterable[Tuple[str, str]]) -> Tuple[Tuple[str, str], ...]:
    result = set()
    for kind, value in values:
        if kind not in ORDER or not isinstance(value, str) or not value or len(value) > 256 or "\0" in value:
            raise Unavailable("invalid permit resource")
        result.add((kind, value))
    return tuple(sorted(result, key=lambda r: (ORDER[r[0]], r[1])))


class Permit:
    """Opaque, process/thread-owned semantic interval; never human input authority."""
    def __init__(self, seal, store, domains, requests, handles, growth):
        if seal is not _SEAL:
            raise Unavailable("invalid permit")
        self._seal = seal
        self.store, self.domains, self.requests = store, tuple(domains), dict(requests)
        self._handles = handles
        self._by_resource = {key: [] for key in requests}
        for domain in self.domains:
            offset = self.domains.index(domain) * len(requests)
            for index, key in enumerate(requests):
                self._by_resource[key].append(handles[offset + index])
        self._owner = (os.getpid(), threading.current_thread())
        self._remaining = growth
        self._closed = False
        self._derived = set()

    def check(self) -> None:
        if (self._closed or self._seal is not _SEAL or self._owner[0] != os.getpid()
                or self._owner[1] is not threading.current_thread()):
            raise Unavailable("permit is closed or belongs to another process/thread")

    def covers(self, requested: Sequence[Tuple[str, str]], topology: bool = False) -> bool:
        self.check()
        for key in resources(requested):
            if key in self._derived:
                continue
            mode = self.requests.get(key)
            if mode is None or (topology and key[0] == "W" and mode != fcntl.LOCK_EX):
                return False
        return True

    def descriptors(self, values=None) -> Tuple[int, ...]:
        self.check()
        if values is not None:
            keys = resources(values)
            if not self.covers(keys):
                raise Unavailable("child exceeds its permit")
            return tuple(fd for key in keys for fd in self._by_resource[key])
        return tuple(self._handles)

    def derive(self, workspace: str, values: Iterable[Tuple[str, str]]) -> None:
        """Only the native-client response adapter calls this after an owned move/create."""
        self.check()
        if self.requests.get(("W", workspace)) != fcntl.LOCK_EX:
            raise Unavailable("fresh identifiers require this operation's exclusive workspace")
        fresh = resources(values)
        if any(k not in ("P", "T") for k, _ in fresh):
            raise Unavailable("invalid native derivation")
        # W excludes a competing control/topology acquisition. Reserve the real
        # separate inodes before publishing the returned object to any helper.
        missing = [r for r in fresh if r not in self.requests]
        if missing:
            cost = self._charge(len(missing))
            child = self.store.acquire(self.domains, missing)
            self._handles.extend(child._handles)
            self.requests.update(child.requests)
            self._by_resource.update(child._by_resource)
            self._remaining -= cost
            child._handles = []
            child.close()

    def created_workspace(self, workspace: str) -> None:
        self.check()
        cost = self._charge(1)
        child = self.store.acquire(self.domains, [("W", workspace)], topology=True)
        self._handles.extend(child._handles)
        self.requests.update(child.requests)
        self._by_resource.update(child._by_resource)
        self._remaining -= cost
        child._handles = []
        child.close()

    def _charge(self, resources_count: int) -> int:
        cost = resources_count * len(self.domains)
        if cost > self._remaining or len(self._handles) + cost > 512:
            raise Unavailable("semantic operation growth budget exceeded")
        return cost

    def close(self) -> None:
        if not self._closed:
            self.check()
            self._closed = True
            for fd in reversed(self._handles):
                os.close(fd)  # Never LOCK_UN: an exact child may still share it.
            self._handles = []

    def __enter__(self):
        self.check()
        return self

    def __exit__(self, *args):
        self.close()


class ControlPermit(Permit):
    """Only the authenticated lease's creator worker owns this input capability.

    Inherited descriptors preserve exclusion; they cannot recreate this object
    or borrow its creator process/thread authority. Automation cannot use it.
    """
    def covers(self, requested, topology=False):
        self.check()
        raise Unavailable("a human permit cannot authorize automated operations")

    def control_descriptors(self, domain, terminal):
        self.check()
        if (self.domains != (domain,) or ("T", terminal) not in self.requests
                or sorted(k for k, _ in self.requests) != ["N", "P", "T", "W"]
                or any(mode != (fcntl.LOCK_SH if key[0] == "W" else fcntl.LOCK_EX)
                       for key, mode in self.requests.items())):
            raise Unavailable("human permit does not cover this exact terminal")
        return self.descriptors()


class Store:
    """Fixed-inode coordination root. ``root`` injection is internal/test-only."""
    def __init__(self, root: Optional[str] = None):
        self.root = root

    @staticmethod
    def _directory(parent: int, name: str) -> int:
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
        except FileExistsError:
            pass
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            value = os.fstat(fd)
            if value.st_uid != os.geteuid() or stat.S_IMODE(value.st_mode) != 0o700:
                raise Unavailable("permit directory ownership/permissions are unsafe")
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _root(self) -> int:
        if self.root is None:
            parent = os.path.realpath("/tmp")
            info = os.stat(parent)
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0
                    or not info.st_mode & stat.S_ISVTX or not info.st_mode & stat.S_IWOTH):
                raise Unavailable("OS temporary parent is unsafe")
            parent, name = os.path.split(default_root())
        else:
            parent, name = os.path.split(absolute(self.root))
        fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            return self._directory(fd, name)
        finally:
            os.close(fd)

    def acquire(self, domains: Sequence[Domain], values: Iterable[Tuple[str, str]],
                topology: bool = False, growth: int = 0) -> Permit:
        """Nonblocking producer permit for ordinary semantic operations."""
        return self._acquire(domains, values, topology, growth, False)

    def control(self, domain, values):
        """Internal lease-worker factory, never exposed by CLI/HTTP parameters."""
        keys = resources(values)
        if sorted(k for k, _ in keys) != ["N", "P", "T", "W"]:
            raise Unavailable("control requires one exact W/N/P/T target")
        return self._acquire([domain], keys, False, 0, True)

    def _acquire(self, domains, values, topology, growth, human):
        keys = resources(values)
        domains = sorted(set(domains), key=lambda d: d.api)
        if not domains or not keys:
            raise Unavailable("empty permit plan")
        modes = {key: (fcntl.LOCK_SH if key[0] == "W" else fcntl.LOCK_EX) if human else
                 (fcntl.LOCK_EX if key[0] == "W" and topology else fcntl.LOCK_SH) for key in keys}
        count = len(keys) * len(domains)
        soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
        # Creation may add two identifiers per reserved name; budget that before
        # the first native effect rather than discovering exhaustion halfway up.
        if type(growth) is not int or not 0 <= growth <= 512:
            raise Unavailable("invalid semantic growth budget")
        needed = count + growth * len(domains)
        try:
            opened = len(os.listdir("/dev/fd"))
        except OSError:
            opened = 32
        if needed > 512 or (soft != resource.RLIM_INFINITY and needed + opened + 32 > soft):
            raise Unavailable("permit descriptor budget exceeded")
        handles = []
        root = directory = None
        complete = False
        try:
            root = self._root()
            for domain in domains:
                directory = self._directory(root, domain.key)
                for key in keys:
                    filename = key[0] + "-" + hashlib.sha256(key[1].encode("utf-8")).hexdigest() + ".lock"
                    fd = os.open(filename, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=directory)
                    handles.append(fd)
                    value = os.fstat(fd)
                    if (not stat.S_ISREG(value.st_mode) or value.st_uid != os.geteuid()
                            or stat.S_IMODE(value.st_mode) != 0o600 or value.st_nlink != 1):
                        raise Unavailable("permit lock ownership/type/permissions are unsafe")
                    try:
                        fcntl.flock(fd, modes[key] | fcntl.LOCK_NB)
                    except OSError as exc:
                        if exc.errno in (errno.EACCES, errno.EAGAIN):
                            raise Held("owner control or another topology operation holds this target")
                        raise
                os.close(directory)
                directory = None
            complete = True
            cls = ControlPermit if human else Permit
            return cls(_SEAL, self, domains, modes, handles, growth * len(domains))
        except (OSError, ValueError) as exc:
            raise Unavailable("permit storage unavailable: %s" % exc.__class__.__name__)
        finally:
            if directory is not None:
                os.close(directory)
            if root is not None:
                os.close(root)
            if not complete:
                for fd in reversed(handles):
                    os.close(fd)
