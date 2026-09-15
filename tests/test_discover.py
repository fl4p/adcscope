"""Discovery-hook tests: registration, deduplication, and failure handling.

These cover the seam that replaced the old hard-coded NAT logic. mDNS itself is not exercised
(it needs a live network and the zeroconf package); everything reachable without a network is.
Run directly:

    python tests/test_discover.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discover

PASS = FAIL = 0


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"PASS {name}")
    else:
        FAIL += 1
        print(f"FAIL {name}\n  got  {got}\n  want {want}")


def reset():
    discover._EXTRA_SOURCES.clear()
    discover._NAME_RESOLVERS.clear()


def test_no_hooks_means_no_extra_endpoints():
    reset()
    check("no hooks -> no extra endpoints", discover.extra_endpoints(), [])
    check("no hooks -> no name", discover.resolve_name("h", 24), None)


def test_registered_source_is_used():
    reset()
    discover.register_endpoint_source(lambda: [("10.0.0.1", 24)])
    check("registered source yields its endpoint", discover.extra_endpoints(), [("10.0.0.1", 24)])


def test_same_function_registered_twice_counts_once():
    reset()

    def src():
        return [("10.0.0.1", 24)]

    discover.register_endpoint_source(src)
    discover.register_endpoint_source(src)
    check("re-registering one function is a no-op", len(discover._EXTRA_SOURCES), 1)


def test_two_sources_naming_one_device_dedupe():
    """The same adapter imported under two module spellings registers twice. One physical device
    must still be one candidate, or single-device auto-connect never fires."""
    reset()
    discover.register_endpoint_source(lambda: [("10.0.0.1", 24), ("10.0.0.2", 24)])
    discover.register_endpoint_source(lambda: [("10.0.0.1", 24)])
    check("duplicate endpoints collapse", discover.extra_endpoints(),
          [("10.0.0.1", 24), ("10.0.0.2", 24)])


def test_port_normalised_for_dedupe():
    reset()
    discover.register_endpoint_source(lambda: [("10.0.0.1", 24)])
    discover.register_endpoint_source(lambda: [("10.0.0.1", "24")])
    check("string vs int port is one endpoint", discover.extra_endpoints(), [("10.0.0.1", 24)])


def test_failing_source_does_not_hide_a_working_one():
    reset()

    def boom():
        raise RuntimeError("nope")

    discover.register_endpoint_source(boom)
    discover.register_endpoint_source(lambda: [("10.0.0.9", 24)])
    check("a throwing source does not suppress the others",
          discover.extra_endpoints(), [("10.0.0.9", 24)])


def test_resolver_order_and_failure():
    reset()
    discover.register_name_resolver(lambda h, p: None)          # knows nothing
    discover.register_name_resolver(lambda h, p: 1 / 0)         # throws
    discover.register_name_resolver(lambda h, p: "bench-1")     # answers
    check("resolution falls through None and exceptions", discover.resolve_name("h", 24), "bench-1")


def test_unresolvable_is_none_not_an_exception():
    reset()
    discover.register_name_resolver(lambda h, p: 1 / 0)
    check("an all-failing resolver chain yields None", discover.resolve_name("h", 24), None)


def test_discover_endpoints_merges_extras_without_mdns():
    reset()
    discover.register_endpoint_source(lambda: [("10.0.0.1", 24)])
    discover.register_name_resolver(lambda h, p: "bench-1")
    check("extras carry their resolved hostname",
          discover.discover_endpoints(mdns=False), [("10.0.0.1", 24, "bench-1")])


def test_probe_names_false_skips_resolution():
    reset()
    called = []
    discover.register_endpoint_source(lambda: [("10.0.0.1", 24)])
    discover.register_name_resolver(lambda h, p: called.append(1) or "bench-1")
    got = discover.discover_endpoints(mdns=False, probe_names=False)
    check("probe_names=False yields no hostname", got, [("10.0.0.1", 24, None)])
    check("probe_names=False does not call the resolver", called, [])


def test_choose_endpoint_rules():
    cands = [("10.0.0.1", 24, "fry"), ("10.0.0.2", 24, "flat")]
    check("match picks by hostname substring",
          discover.choose_endpoint(cands, match="fla"), ("10.0.0.2", 24, "flat"))
    check("no match yields None", discover.choose_endpoint(cands, match="zzz"), None)
    check("single candidate auto-picks",
          discover.choose_endpoint([cands[0]]), cands[0])
    check("empty yields None", discover.choose_endpoint([]), None)
    check("non-interactive falls back to index 0",
          discover.choose_endpoint(cands, interactive=False), cands[0])


# --- the fugu adapter, as a worked example of the seam ---------------------------------------

def test_fugu_adapter_port_arithmetic():
    reset()
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                    "contrib"))
    os.environ["NAT_TELNET"] = "192.168.1.231:232,192.168.1.231:233"
    import fugu_nat                                      # noqa: F401  registers both hooks
    check("telnet 23x -> scope 24x (+10)", discover.extra_endpoints(),
          [("192.168.1.231", 242), ("192.168.1.231", 243)])


if __name__ == "__main__":
    for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
        fn()
    reset()
    print(f"\n{PASS}/{PASS + FAIL} passed")
    sys.exit(1 if FAIL else 0)
