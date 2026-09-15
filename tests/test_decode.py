"""Wire-decoder tests: 12-bit unpacking, and that a sample split across TCP reads survives.

A TCP read is not a message. The decoder used to drop an odd trailing byte, which resyncs the
stream onto the wrong byte boundary and yields plausible wrong values with no error — the worst
failure this tool can have, because a wrong trace is read as a real measurement. Run directly:

    python tests/test_decode.py
"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import adcscope as fs

PASS = FAIL = 0


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"PASS {name}")
    else:
        FAIL += 1
        print(f"FAIL {name}\n  got  {got}\n  want {want}")


def _state():
    return fs.ScopeState(types.SimpleNamespace(match=None, ip=None, port=24, rate=2000,
                                               median=False))


def _decode(chunks, with_header=False):
    """Feed `chunks` to a fresh Decoder; return {cid: [values]} oldest-first."""
    s = _state()
    if not with_header:
        for cid, nm in ((0, "a"), (1, "b"), (2, "c")):
            s.add_channel(cid, nm, "u", 12)
    d = fs.Decoder(s)
    for c in chunks:
        d.decode(c, 0.0)
    return _samples(s)


def _samples(s):
    """{cid: [values]} oldest-first, omitting channels that received nothing (their ring is
    NaN-filled, not empty)."""
    out = {}
    for cid, ch in s.channels.items():
        vals = [int(v) for v in ch.ordered() if v == v]     # NaN != NaN
        if vals:
            out[cid] = vals
    return out


def pack12(cid, val):
    """Encode one 12-bit sample the way the firmware's Data12Ch4 bitfield lays it out."""
    return bytes([((cid & 0x07) << 1) | ((val & 0x0F) << 4), (val >> 4) & 0xFF])


# --- layout ---------------------------------------------------------------------------------

def test_pack_matches_firmware_abi():
    # verified against a host compile of the firmware struct: cid=5, val=0xABC -> CA AB
    check("pack12 matches the firmware bitfield ABI", pack12(5, 0xABC).hex(), "caab")


def test_roundtrip_single_channel():
    data = b"".join(pack12(1, v) for v in (0, 1, 2047, 4095))
    check("12-bit round-trip", _decode([data])[1], [0, 1, 2047, 4095])


# --- fragmentation --------------------------------------------------------------------------

DATA = bytes([0x20, 0x12, 0x40, 0x80, 0x60, 0x24])   # the reviewer's reproducer


def test_whole_read():
    # b0 = 0x20/0x40/0x60 all mask to cid 0; values are (b1 << 4) | (b0 >> 4)
    check("whole read", _decode([DATA]), {0: [290, 2052, 582]})


def test_split_mid_sample():
    """3+3 splits the second sample. Every split must give the same result as one read."""
    check("split 3+3 (mid-sample)", _decode([DATA[:3], DATA[3:]]), _decode([DATA]))


def test_every_split_point_agrees():
    want = _decode([DATA])
    for i in range(1, len(DATA)):
        got = _decode([DATA[:i], DATA[i:]])
        if got != want:
            check(f"split {i}+{len(DATA)-i}", got, want)
            return
    check("all split points agree with one read", True, True)


def test_byte_at_a_time():
    check("one byte per read", _decode([DATA[i:i + 1] for i in range(len(DATA))]), _decode([DATA]))


# --- header ---------------------------------------------------------------------------------

HEAD = b"###ScopeHead:@host=bench-1,0$vin=u12,1$vout=u12,###ENDHEAD\n"


def test_header_then_samples():
    s = _state()
    d = fs.Decoder(s)
    d.decode(HEAD, 0.0)
    check("header names the host", s.hostname, "bench-1")
    check("header declares channels", sorted(s.channels), [0, 1])


def test_header_split_across_reads():
    s = _state()
    d = fs.Decoder(s)
    for i in (7, 20, len(HEAD) - 3):
        s2, d2 = _state(), None
        d2 = fs.Decoder(s2)
        d2.decode(HEAD[:i], 0.0)
        d2.decode(HEAD[i:], 0.0)
        if sorted(s2.channels) != [0, 1] or s2.hostname != "bench-1":
            check(f"header split at {i}", (s2.hostname, sorted(s2.channels)),
                  ("bench-1", [0, 1]))
            return
    check("header survives every split point tried", True, True)


def test_samples_coalesced_after_header():
    """The firmware writes the header separately, but TCP may deliver it glued to samples."""
    s = _state()
    d = fs.Decoder(s)
    d.decode(HEAD + pack12(0, 1234) + pack12(1, 77), 0.0)
    check("samples coalesced after the header are not dropped", _samples(s), {0: [1234], 1: [77]})


def test_reset_drops_stale_residue():
    s = _state()
    for cid, nm in ((0, "a"), (1, "b")):
        s.add_channel(cid, nm, "u", 12)
    d = fs.Decoder(s)
    d.decode(pack12(0, 100) + b"\x20", 0.0)     # trailing half-sample carried
    d.reset()
    d.decode(pack12(1, 55), 0.0)                 # new connection: must not fuse with the residue
    check("reset() drops carried residue", _samples(s).get(1), [55])


if __name__ == "__main__":
    for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
        fn()
    print(f"\n{PASS}/{PASS + FAIL} passed")
    sys.exit(1 if FAIL else 0)
