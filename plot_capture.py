#!/usr/bin/env python3
"""Render an identification thumbnail (img.webp) for each saved scope capture folder.

A capture folder holds one file per channel: `<ch>.csv`, `<ch>_<SR>hz.csv` or `<ch>_<SR>.npz`
(npz holds raw int16 under key `v`; rate is in the filename, see adcscope.py). This overlays all
channels on a shared real-time axis (per-channel SR) in raw ADC counts, matching china-inverter-1kw-load/img.webp.

Captures live in the fugu-data repo, checked out at scope_client/data/ (gitignored here); the
default run walks that tree.

  python plot_capture.py                  # all capture dirs under data/ (fugu-data)
  python plot_capture.py <dir> [<dir>..]  # specific folders
  python plot_capture.py --force          # overwrite existing img.webp
"""
import os, re, sys
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

CHANNELS = ("vin", "vin1", "iin", "iout", "vout", "vout_filt", "ntc", "ucTemp")
COLORS = {"vin": "tab:blue", "vin1": "tab:cyan", "iin": "tab:olive", "iout": "tab:orange",
          "vout": "tab:red", "vout_filt": "tab:brown", "ntc": "tab:green", "ucTemp": "tab:purple"}
_RE = re.compile(r"^(" + "|".join(CHANNELS) + r")(?:_(\d+(?:\.\d+)?)(?:hz)?)?$", re.IGNORECASE)


def parse(fname):
    """(channel, rate|None) from a capture filename, or None if it isn't a channel file."""
    stem = fname.rsplit(".", 1)[0]
    m = _RE.match(stem)
    if not m:
        return None
    return m.group(1), (float(m.group(2)) if m.group(2) else None)


def load_vals(path):
    if path.endswith(".npz"):
        z = np.load(path)
        return (z["v"] if "v" in z else z[z.files[0]]).astype(np.float64)
    out = []
    with open(path) as f:
        first = True
        for line in f:
            s = line.strip().strip('"')
            if first:                      # drop a non-numeric header (e.g. '0' index header is numeric -> kept)
                first = False
                try: float(s)
                except ValueError: continue
            try: out.append(float(s))
            except ValueError: out.append(np.nan)
    return np.asarray(out, np.float64)


def collect(folder):
    """{channel: (vals, rate|None)} preferring .npz, then highest-rate file per channel."""
    found = {}
    for f in sorted(os.listdir(folder)):
        if not f.endswith((".csv", ".npz")):
            continue
        p = parse(f)
        if not p:
            continue
        ch, rate = p
        prev = found.get(ch)
        better = prev is None or (f.endswith(".npz") and not prev[2].endswith(".npz"))
        if better:
            found[ch] = (load_vals(os.path.join(folder, f)), rate, f)
    return {ch: (v, r) for ch, (v, r, _) in found.items()}


def plot(folder, force=False):
    out = os.path.join(folder, "img.webp")
    if os.path.exists(out) and not force:
        return f"skip (exists): {folder}"
    chans = collect(folder)
    if not chans:
        return None
    # reference duration from channels with a known rate -> spread rate-less/0-rate channels across it
    durs = [(len(v) - 1) / r for v, r in chans.values() if r and r > 0 and len(v) > 1]
    Tref = max(durs) if durs else 1.0
    fig, ax = plt.subplots(figsize=(11, 4.2))
    for ch in CHANNELS:
        if ch not in chans:
            continue
        v, r = chans[ch]
        n = len(v)
        if n < 2:
            continue
        sr = r if (r and r > 0) else (n - 1) / Tref
        t = np.arange(n) / sr
        ax.plot(t, v, lw=0.7, color=COLORS.get(ch), label=f"{ch}" + (f" ({r:.0f}Hz)" if r else ""))
    ax.set_xlabel("Time (s)")
    ax.set_ylabel("raw ADC counts")
    ax.set_title(os.path.relpath(folder, DATA_ROOT))
    ax.grid(True, alpha=0.3)
    ax.legend(loc="upper right", fontsize=8, ncol=2)
    fig.tight_layout()
    fig.savefig(out, format="webp", dpi=110)
    plt.close(fig)
    return f"wrote {out}  ({len(chans)} ch, {Tref:.2f}s)"


ROOT = os.path.dirname(os.path.abspath(__file__))
# Captures live in the standalone fugu-data repo, checked out at scope_client/data/ (gitignored from
# the firmware repo). Default to walking that tree so re-runs only touch fugu-data captures.
DATA_ROOT = os.path.join(ROOT, "data") if os.path.isdir(os.path.join(ROOT, "data")) else ROOT


def walk(root):
    for dirpath, _, files in os.walk(root):
        if "__pycache__" in dirpath:
            continue
        if any(parse(f) for f in files if f.endswith((".csv", ".npz"))):
            yield dirpath


def main(argv):
    force = "--force" in argv
    args = [a for a in argv if not a.startswith("--")]
    folders = args if args else sorted(walk(DATA_ROOT))
    for folder in folders:
        try:
            r = plot(folder, force)
            if r:
                print(r)
        except Exception as e:
            print(f"FAIL {folder}: {e}")


if __name__ == "__main__":
    main(sys.argv[1:])
