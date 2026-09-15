"""Round-trip tests for adcscope capture save/load (save_npz/save_csv -> load_capture) and the
filename/dir helpers. Pure logic, no GUI window. Run directly:

    python etc/scope_client/test_capture_io.py
"""
import os
import sys
import tempfile
import time
import types
import glob

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import adcscope as fs


def _state(host="fugu-esp32s3-1"):
    s = fs.ScopeState(types.SimpleNamespace(match=None, ip=None, port=24, rate=2000, median=False))
    s.hostname = host
    return s


def _feed(state, cid, name, typ, values, rate=2000.0, batch=100):
    """Add a channel and push `values` (a list of ints) in fixed-rate batches so its reconstructed
    sample rate is `rate`."""
    state.add_channel(cid, name, typ, 12)
    ch = state.channels[cid]
    gap = batch / rate
    t = time.time()
    for i in range(0, len(values), batch):
        t += gap
        ch.add_batch(values[i:i + batch], t, state.dt)
    return ch


def _in_tmp():
    d = tempfile.mkdtemp()
    os.chdir(d)
    return d


def test_filename_helpers():
    assert fs._sr_from_name("vin_2000.npz") == 2000.0
    assert fs._sr_from_name("ucTemp_5.csv") == 5.0
    assert fs._sr_from_name("noview.json") is None
    assert fs._stem_name("vin_2000.npz") == "vin"
    assert fs._stem_name("foo_bar_100.csv") == "foo_bar"        # name keeps its own underscores


def test_capture_epoch_parses_dir():
    ep = fs._capture_epoch("h/20260530T105907Z/vin_2000.npz")
    assert ep is not None and 1.7e9 < ep < 2.0e9, ep
    assert fs._capture_epoch("h/not-a-date/vin.npz") is None


def test_npz_is_values_only():
    _in_tmp()
    st = _state()
    _feed(st, 1, "vin", "u", [int(x) for x in np.random.default_rng(0).integers(0, 4096, 500)])
    fs.save_npz(st)
    f = glob.glob("*/*/vin_*.npz")[0]
    assert sorted(np.load(f).keys()) == ["v"], "npz must hold only the value array"
    assert np.load(f)["v"].dtype == np.int16


def test_npz_roundtrip_values_and_view():
    _in_tmp()
    rng = np.random.default_rng(1)
    vin = [int(x) for x in rng.integers(0, 4096, 600)]
    iin = [int(x) for x in rng.integers(-60, 60, 600)]            # signed channel
    st = _state()
    cv = _feed(st, 1, "vin", "u", vin)
    _feed(st, 2, "iin", "i", iin)
    cv.scale, cv.offset, cv.coupling = 2.5, -2048.0, "AC"
    fs.save_npz(st)
    f = glob.glob("*/*/vin_*.npz")[0]
    sr = fs._sr_from_name(os.path.basename(f))
    assert sr

    st2 = _state("other")
    fs.load_capture(st2, f)
    assert st2.frozen and st2.requested is None and st2.scene_dirty
    by = {c.name: c for c in st2.channel_list()}
    assert np.array_equal(by["vin"].recent(len(vin))[0].astype(np.int16), np.asarray(vin, np.int16))
    assert np.array_equal(by["iin"].recent(len(iin))[0].astype(np.int16), np.asarray(iin, np.int16))
    assert by["iin"].recent(len(iin))[0].min() < 0, "signed values preserved"
    # timing reconstructed from filename SR
    assert abs(by["vin"].dt_ch - 1.0 / sr) < 1e-9
    # view restored from sidecar
    assert (by["vin"].scale, round(by["vin"].offset, 3), by["vin"].coupling) == (2.5, -2048.0, "AC")
    # both channels end-aligned to the capture epoch
    ends = {round(c.ts_last, 3) for c in st2.channel_list()}
    assert len(ends) == 1 and abs(st2.frozen_now - max(ends)) < 1e-3


def test_view_sidecar_written():
    import json
    _in_tmp()
    st = _state()
    c = _feed(st, 1, "vin", "u", [1, 2, 3, 4])
    c.scale, c.coupling = 0.1, "AC"
    fs.save_npz(st)
    sidecar = glob.glob("*/*/view.json")[0]
    v = json.load(open(sidecar))
    assert v["vin"]["scale"] == 0.1 and v["vin"]["coupling"] == "AC"


def test_csv_roundtrip_values():
    _in_tmp()
    rng = np.random.default_rng(2)
    vals = [int(x) for x in rng.integers(0, 4096, 400)]
    st = _state()
    _feed(st, 1, "vin", "u", vals)
    fs.save_csv(st)
    f = glob.glob("*/*/vin_*.csv")[0]
    st2 = _state("x")
    fs.load_capture(st2, f)
    c = [x for x in st2.channel_list() if x.name == "vin"][0]
    assert np.array_equal(c.recent(len(vals))[0].astype(np.float32), np.asarray(vals, np.float32))


def test_macos_single_glfw_env():
    if sys.platform != "darwin":
        return
    lib = os.environ.get("PYGLFW_LIBRARY")
    assert lib and os.path.exists(lib) and "imgui_bundle" in lib, \
        "PYGLFW_LIBRARY should point at imgui_bundle's bundled libglfw to avoid the dual-GLFW input bug"


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
        except Exception as e:
            failed += 1
            print("ERROR", t.__name__, "-", repr(e))
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
