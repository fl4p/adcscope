"""adcscope discovery adapter for NAT-routed fugu MPPT boards.

Worked example of the two `discover` hooks. Boards behind the lab's NAT router are not
mDNS-reachable, so their endpoints come from `nat.env`'s `$NAT_TELNET` (host:port,
comma-separated) instead. The router forwards telnet on `23x` and scope on `24x`, mirroring
the device ports (23 vs 24), so each scope endpoint is a telnet endpoint's port + 10 — and the
same offset run backwards gives the telnet port whose welcome banner names the board.

Import it to activate:

    import contrib.fugu_nat   # registers both hooks with `discover`

`NAT_ENV` defaults to `nat.env` beside this file; point `$NAT_TELNET` at the endpoints
directly, or set `fugu_nat.NAT_ENV` before the first call, to use it elsewhere.
"""
import os

from discover import register_endpoint_source, register_name_resolver

NAT_ENV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nat.env")
SCOPE_PORT_OFFSET = 10   # forwarded scope port = forwarded telnet port + 10 (23x -> 24x)


def _load_env_file(path):
    """Fill os.environ from a KEY=VALUE file (shell-set vars win); silent if absent."""
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    os.environ.setdefault(k.strip(), v.strip())
    except FileNotFoundError:
        pass


@register_endpoint_source
def nat_scope_endpoints():
    """List of (host, port) scope endpoints from `$NAT_TELNET` (read from nat.env if unset);
    empty when none are configured."""
    if not os.environ.get("NAT_TELNET"):
        _load_env_file(NAT_ENV)
    out = []
    for ep in (os.environ.get("NAT_TELNET") or "").split(","):
        ep = ep.strip()
        host, _, port = ep.partition(":")
        if ep and port:
            out.append((host.strip(), int(port) + SCOPE_PORT_OFFSET))
    return out


@register_name_resolver
def probe_telnet_hostname(host, scope_port, timeout=2.0):
    """Hostname from the device's telnet welcome banner ("Welcome to <host> ..."), reusing
    `fugu_console.probe_welcome`. The telnet console sits one port-block below the scope service
    (23x vs 24x), so probing it never disturbs the scope stream. None if unreachable / no banner /
    `fugu_console` is unavailable."""
    import asyncio
    try:
        try:
            from fugu_console import probe_welcome
        except ImportError:
            from etc.fugu_console import probe_welcome
    except Exception:
        return None
    try:
        return asyncio.run(probe_welcome(host, scope_port - SCOPE_PORT_OFFSET, timeout))
    except Exception:
        return None
