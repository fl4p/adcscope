"""Unit test for the adcscope device-selection state machine (ScopeState auto-pick rules).

Pure logic, no GUI/socket — just exercises set_candidates / select / disconnect. Run directly:

    python etc/scope_client/test_device_select.py
"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from adcscope import ScopeState

FRY = ("192.168.1.231", 233, None)        # NAT endpoint, hostname unknown until header
FLAT = ("192.168.1.231", 232, None)
BENCH = ("192.168.4.7", 24, "fugu-esp32s3-1")


def _args(match=None, ip=None):
    return types.SimpleNamespace(match=match, ip=ip, port=24, rate=2000, median=False)


def _state(match=None):
    return ScopeState(_args(match=match))


def test_single_candidate_autoconnects():
    s = _state()
    s.set_candidates([BENCH])
    assert s.requested == BENCH, "a lone device should be auto-picked"


def test_multiple_candidates_wait_for_user():
    s = _state()
    s.set_candidates([FRY, FLAT, BENCH])
    assert s.requested is None, "ambiguous discovery must wait for a manual pick"


def test_match_autopicks_first_even_when_many():
    s = _state(match="fugu")
    s.set_candidates([BENCH, FRY])
    assert s.requested == BENCH, "--match narrows enough to auto-pick the first candidate"


def test_user_pick_sticks_across_rediscovery():
    s = _state()
    s.set_candidates([FRY, FLAT])
    s.select(FLAT)
    assert s.requested == FLAT and s.user_picked
    s.set_candidates([FRY, FLAT, BENCH])      # a new device appears
    assert s.requested == FLAT, "auto-pick must never override a manual choice"


def test_disconnect_then_no_autoreconnect():
    s = _state()
    s.set_candidates([BENCH])
    assert s.requested == BENCH
    s.disconnect()
    assert s.requested is None and s.user_picked
    s.set_candidates([BENCH])                 # still the only device
    assert s.requested is None, "disconnect is sticky; don't silently reconnect"


def test_rejected_endpoint_skipped_by_autopick():
    s = _state()
    s.rejected.add((FRY[0], FRY[1]))          # e.g. --match mismatch discovered mid-stream
    s.set_candidates([FRY])
    assert s.requested is None, "a rejected endpoint is not auto-picked"
    s.set_candidates([FRY, BENCH])
    assert s.requested == BENCH, "only the non-rejected single candidate is picked"


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print("PASS", t.__name__)
        except AssertionError as e:
            failed += 1
            print("FAIL", t.__name__, "-", e)
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
