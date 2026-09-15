"""Scope-endpoint discovery + selection.

The only discovery mechanism that is part of the protocol is mDNS: a device advertises its
scope server as `_scope._tcp`, and the advert carries both the address and the hostname.

Anything else is deployment-specific, so it enters through two hooks rather than being
hard-coded here:

  * `register_endpoint_source(fn)` — `fn() -> [(host, port)]` for endpoints mDNS cannot see
    (a board behind a NAT router, a fixed lab address, a tunnel).
  * `register_name_resolver(fn)` — `fn(host, port) -> hostname | None` for naming those
    endpoints, since they have no advert to read a hostname from.

`contrib/fugu_nat.py` is a worked example of both (NAT-forwarded boards named from a telnet
welcome banner). Without any registered hook this module discovers by mDNS alone.
"""
import threading
import time

# Deployment-specific endpoint providers and hostname resolvers; see module docstring.
_EXTRA_SOURCES = []
_NAME_RESOLVERS = []


def register_endpoint_source(fn):
    """Register `fn() -> [(host, port)]` yielding scope endpoints mDNS cannot advertise."""
    _EXTRA_SOURCES.append(fn)
    return fn


def register_name_resolver(fn):
    """Register `fn(host, port) -> hostname | None` for endpoints with no mDNS advert."""
    _NAME_RESOLVERS.append(fn)
    return fn


def extra_endpoints():
    """Union of every registered endpoint source; empty when none are registered."""
    out = []
    for fn in _EXTRA_SOURCES:
        try:
            out.extend(fn() or ())
        except Exception as e:
            print("endpoint source failed:", e)
    return out


def resolve_name(host, port):
    """First non-None hostname from the registered resolvers, else None."""
    for fn in _NAME_RESOLVERS:
        try:
            name = fn(host, port)
            if name:
                return name
        except Exception:
            pass
    return None


def discover_mdns(timeout=2.0):
    """One-shot mDNS sweep. `_scope._tcp` adverts as (host, port, hostname|None)."""
    from zeroconf import Zeroconf, ServiceBrowser, ServiceListener

    found = []

    class _Listener(ServiceListener):
        def update_service(self, zc, type_, name):
            pass

        def remove_service(self, zc, type_, name):
            pass

        def add_service(self, zc, type_, name):
            info = zc.get_service_info(type_, name)
            if not info:
                return
            host = (info.server or name).rstrip(".")
            host = host[:-6] if host.endswith(".local") else host
            found.extend((a, info.port, host) for a in info.parsed_addresses())

    zc = Zeroconf()
    try:
        ServiceBrowser(zc, "_scope._tcp.local.", _Listener())
        time.sleep(timeout)
    finally:
        zc.close()
    return found


def discover_endpoints(mdns=True, probe_names=True, timeout=2.0):
    """All scope endpoints as (host, port, hostname|None): mDNS adverts first (hostname from the
    advert), then registered extra sources (hostname via the registered resolvers when
    `probe_names`)."""
    cand = []
    if mdns:
        try:
            cand += discover_mdns(timeout)
        except ImportError:
            pass
        except Exception as e:
            print("mDNS discovery failed:", e)
    seen = {(h, p) for h, p, _ in cand}
    extra = [(h, p) for h, p in extra_endpoints() if (h, p) not in seen]
    if probe_names and extra:
        names = {}                       # probe all at once (each blocks up to timeout)

        def _probe(h, p):
            names[(h, p)] = resolve_name(h, p)

        threads = [threading.Thread(target=_probe, args=hp, daemon=True) for hp in extra]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        cand += [(h, p, names.get((h, p))) for h, p in extra]
    else:
        cand += [(h, p, None) for h, p in extra]
    return cand


class ScopeDiscovery:
    """Live scope-endpoint discovery for long-running clients (adcscope.py).

    Holds one persistent mDNS `_scope._tcp` browser, so repeated `snapshot()` calls are free and
    emit no per-sweep Zeroconf open/close chatter. Endpoints from registered extra sources are
    merged in and their hostnames resolved once, cached, and re-probed at most every
    `probe_interval` s while still unknown. `note_hostname()` lets a connected client feed back the
    hostname it learned from the scope header, suppressing further probes for it.
    """

    def __init__(self, mdns=True, probe_names=True, probe_interval=20.0):
        self._lock = threading.Lock()
        self._mdns = {}              # service name -> [(host, port, hostname)]
        self._names = {}             # (host, port) -> hostname
        self._next = {}              # (host, port) -> monotonic deadline for the next probe
        self._probe_names = probe_names
        self._probe_interval = probe_interval
        self._zc = None
        self._browser = None
        if mdns:
            self._start_mdns()

    def _start_mdns(self):
        try:
            from zeroconf import Zeroconf, ServiceBrowser
        except Exception as e:
            print("mDNS unavailable:", e)
            return
        self._zc = Zeroconf()
        self._browser = ServiceBrowser(self._zc, "_scope._tcp.local.", handlers=[self._on_change])

    def _on_change(self, zeroconf, service_type, name, state_change):
        from zeroconf import ServiceStateChange
        if state_change is ServiceStateChange.Removed:
            with self._lock:
                self._mdns.pop(name, None)
            return
        info = zeroconf.get_service_info(service_type, name, timeout=1000)
        if not info:
            return
        host = (info.server or name).rstrip(".")
        host = host[:-6] if host.endswith(".local") else host
        eps = [(a, info.port, host) for a in info.parsed_addresses()]
        with self._lock:
            self._mdns[name] = eps

    def note_hostname(self, host, port, name):
        """Record a hostname learned out-of-band (e.g. a connected client's scope header)."""
        if name:
            with self._lock:
                self._names[(host, port)] = name

    def snapshot(self):
        """Current endpoints as (host, port, hostname|None): live mDNS adverts first, then the
        registered extra endpoints with their probed/cached hostname (None until first reached)."""
        with self._lock:
            cand = [ep for eps in self._mdns.values() for ep in eps]
            names, nexts = dict(self._names), dict(self._next)
        seen = {(h, p) for h, p, _ in cand}
        now = time.monotonic()
        for h, p in extra_endpoints():
            if (h, p) in seen:
                continue
            name = names.get((h, p))
            if name is None and self._probe_names and now >= nexts.get((h, p), 0.0):
                name = resolve_name(h, p)
                with self._lock:
                    self._next[(h, p)] = now + self._probe_interval
                    if name:
                        self._names[(h, p)] = name
            cand.append((h, p, name))
        return cand

    def close(self):
        if self._zc is not None:
            self._zc.close()


def choose_endpoint(candidates, match=None, interactive=True):
    """Pick one (host, port, hostname) from `candidates`.

    With `match`, auto-pick the first whose hostname contains it. With a single candidate, auto-pick.
    Otherwise print a numbered menu (hostname when known, else host:port) and read the choice from
    stdin; falls back to index 0 on empty/invalid input or when not `interactive`. None if empty.
    """
    if not candidates:
        return None
    if match:
        for c in candidates:
            if c[2] and match in c[2]:
                return c
        print(f"no discovered host matches {match!r}")
        return None
    if len(candidates) == 1:
        return candidates[0]
    for i, (h, p, name) in enumerate(candidates):
        print(f"  [{i}] {name or '?':<24} {h}:{p}")
    if not interactive:
        return candidates[0]
    try:
        sel = input(f"connect to which? [0-{len(candidates) - 1}] (0): ").strip()
    except EOFError:
        return candidates[0]
    try:
        return candidates[int(sel)] if sel else candidates[0]
    except (ValueError, IndexError):
        print("invalid selection; using 0")
        return candidates[0]
