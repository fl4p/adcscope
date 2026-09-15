"""
GPU oscilloscope client for microcontroller ADCs.

Connects to a device's scope service (TCP port 24, mDNS _scope._tcp; wire format in
PROTOCOL.md), decodes the 12-bit sample stream into a 100k-sample-per-channel ring
buffer, and draws it like a real oscilloscope with fastplotlib (WGPU/pygfx).

Channels stream at different (and not exactly known) sample rates and arrive in TCP
batches, so each channel tracks the wall-clock arrival time of its newest sample and
estimates the timestamps of older samples from its measured average period. The plot's
x-axis is therefore real time (seconds), shared across channels of different rates.

  * sliders set the horizontal time window and the vertical range (slider-driven view)
  * each channel has its own scale (gain), offset (position) and coupling (DC / AC)
  * AC coupling removes the DC component averaged over the visible interval
  * edge trigger with auto/normal mode, pre-trigger position, and a dotted level line
    you can drag with the mouse; the trigger follows the source channel's coupling

Device discovery runs continuously in a background thread (--discover-interval, default 3 s; the
"Rescan" button forces a sweep). With a single device found the receive loop auto-connects; with
several it waits and the control panel's "Devices" list lets you pick one (and switch live, or
"Disconnect"). --ip pins a target and skips discovery; --match restricts auto-pick to hostnames
containing the given substring.

Capture save/load (bottom button row):
  * "save NPZ"/"save CSV" write one file per channel under `<hostname>/<datetime>/<ch>_<SR>.{npz,csv}`
    plus a `view.json` sidecar (per-channel scale/offset/coupling). The npz holds only the raw int16
    samples (key `v`); timing is reconstructed on load from the `_<SR>` rate in the filename and the
    capture time in the directory name, so no timestamps are stored.
  * "load..." opens a file dialog; picking any file loads the whole capture directory, freezes live
    streaming and plots it (the view anchors on the data's end). Click a device to go live again.
  * "clear buf" empties all sample buffers.

Connection + wire-format handling is modelled on the legacy scope-client.py reference.

  python -m adcscope                 # discover via mDNS + nat.env, pick in the UI
  python -m adcscope -m fry          # auto-pick a discovered device by hostname
  python -m adcscope --ip 192.168.4.2 [--port 24]

Devices mDNS cannot reach (behind a NAT router, say) are supplied by whatever discovery hooks
the caller registered — see discover.py and contrib/fugu_nat.py for a worked example.
"""
import argparse
import atexit
import collections
import os
import signal
import socket
import sys
import threading
import time

import numpy as np
import yaml

# allow running from anywhere (tests import this from tests/)
_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from discover import ScopeDiscovery

# macOS: fastplotlib's canvas (rendercanvas -> `glfw` pip pkg) and imgui_bundle each ship their own
# libglfw. Loading both registers the GLFW NSApp/window delegates twice and mouse events route to the
# wrong (unused) GLFW, so the window renders but the controls never get input. Point the `glfw` pkg at
# imgui_bundle's bundled dylib *before* fastplotlib imports it, so only one GLFW is ever loaded.
if sys.platform == "darwin" and not os.environ.get("PYGLFW_LIBRARY"):
    try:
        import imgui_bundle
        _glfw_lib = os.path.join(os.path.dirname(imgui_bundle.__file__), "libglfw.3.dylib")
        if os.path.exists(_glfw_lib):
            os.environ["PYGLFW_LIBRARY"] = _glfw_lib
    except Exception:
        pass

import fastplotlib as fpl
from fastplotlib.ui import EdgeWindow
from imgui_bundle import imgui

CHANNEL_COLORS = ["magenta", "cyan", "yellow", "green", "red", "white", "orange", "blue"]
GRID_COLOR = "#444444"
TRIGGER_COLOR = "#ff5555"
BUF_SAMPLES = 200_000          # per-channel sample buffer depth
COUPLINGS = ["DC", "AC"]
SETTINGS_PATH = os.path.join(os.path.dirname(__file__), "adcscope.yaml")


class Channel:
    def __init__(self, cid, name, typ, bitlen, median):
        self.cid = cid
        self.name = name
        self.typ = typ                       # 'u' | 'i' | 'f'
        self.bitlen = bitlen
        self.sign_mask = (1 << bitlen) if typ == 'i' else 0
        self.half = 1 << (bitlen - 1)
        self.ring = np.full(BUF_SAMPLES, np.nan, dtype=np.float32)
        self.ts = np.full(BUF_SAMPLES, np.nan, dtype=np.float64)   # per-sample wall-clock
        self.head = 0
        self.n_samples = 0
        self.t_first = 0.0
        self.dt_ch = 0.0                     # estimated sample period
        self.ts_last = 0.0                   # timestamp assigned to the newest sample
        self._t_prev = 0.0                   # arrival time of the previous batch
        self.line = None
        self.visible = True
        self.scale = 1.0
        self.coupling = "DC"
        self.offset = -float(self.half) if typ == 'u' else 0.0   # center unipolar DC
        self._med = collections.deque(maxlen=5) if median else None
        self._ndrawn = 0

    def add_batch(self, vals, t_now, fallback_dt):
        """Append a batch of samples that arrived together at t_now. Each sample gets a
        stable timestamp (never revised later); a gentle clock-recovery loop keeps the
        newest timestamp tracking the wall clock without snapping, so older samples don't
        jump when a new block arrives."""
        K = len(vals)
        if K == 0:
            return
        a = np.asarray(vals, dtype=np.float32)
        if self.sign_mask:
            a[a >= self.half] -= self.sign_mask
        if self._med is not None:
            for j in range(K):
                self._med.append(float(a[j]))
                a[j] = np.median(self._med)

        if self.t_first == 0.0:
            self.t_first = t_now
            self.dt_ch = fallback_dt
            self._t_prev = t_now
            prev_ts = t_now - K * self.dt_ch
            self.ts_last = t_now
        else:
            gap = t_now - self._t_prev
            self._t_prev = t_now
            if gap > 0:
                self.dt_ch += 0.1 * (gap / K - self.dt_ch)      # EWMA of true period
            prev_ts = self.ts_last
            naive = self.ts_last + K * self.dt_ch
            self.ts_last = naive + 0.2 * (t_now - naive)        # gentle resync, no snap
            if self.ts_last <= prev_ts:                         # keep monotonic
                self.ts_last = prev_ts + K * self.dt_ch
        ts = prev_ts + (np.arange(1, K + 1) / K) * (self.ts_last - prev_ts)

        pos = self.head % BUF_SAMPLES
        end = pos + K
        if end <= BUF_SAMPLES:
            self.ring[pos:end] = a
            self.ts[pos:end] = ts
        else:
            f = BUF_SAMPLES - pos
            self.ring[pos:] = a[:f]; self.ts[pos:] = ts[:f]
            self.ring[:end - BUF_SAMPLES] = a[f:]; self.ts[:end - BUF_SAMPLES] = ts[f:]
        self.head += K
        self.n_samples += K

    def dt_est(self, fallback):
        return self.dt_ch if self.dt_ch > 0 else fallback

    def clear(self):
        """Drop all buffered samples + the clock-recovery state, keeping the view settings
        (scale/offset/coupling/visibility/line)."""
        self.ring.fill(np.nan)
        self.ts.fill(np.nan)
        self.head = self.n_samples = 0
        self.t_first = self.dt_ch = self.ts_last = self._t_prev = 0.0
        if self._med is not None:
            self._med.clear()
        self._ndrawn = 0

    def recent(self, k):
        """Newest k (value, timestamp) samples, oldest..newest."""
        n = min(self.head, BUF_SAMPLES)
        k = min(k, n)
        pos = self.head % BUF_SAMPLES
        if k <= pos:
            return self.ring[pos - k:pos], self.ts[pos - k:pos]
        f = k - pos
        return (np.concatenate((self.ring[BUF_SAMPLES - f:], self.ring[:pos])),
                np.concatenate((self.ts[BUF_SAMPLES - f:], self.ts[:pos])))

    def ordered(self):
        """Whole buffer, oldest..newest (NaN-padded at front until full)."""
        p = self.head % BUF_SAMPLES
        return np.concatenate((self.ring[p:], self.ring[:p]))

    def sample_rate(self):
        return 1.0 / self.dt_ch if self.dt_ch > 0 else 0.0

    def window(self, t_anchor, x_left, x_right, fallback_dt):
        """Samples with a stored timestamp in [t_anchor+x_left, t_anchor+x_right].
        Returns (xs, ys) with xs in seconds relative to t_anchor, oldest..newest, or None."""
        n = min(self.head, BUF_SAMPLES)
        if n < 2:
            return None
        dt = self.dt_est(fallback_dt)
        need = int((self.ts_last - (t_anchor + x_left)) / dt) + 8
        vals, ts = self.recent(int(np.clip(need, 16, n)))
        lo = np.searchsorted(ts, t_anchor + x_left, "left")
        hi = np.searchsorted(ts, t_anchor + x_right, "right")
        if hi - lo < 1:
            return None
        return (ts[lo:hi] - t_anchor).astype(np.float32), vals[lo:hi]


class ScopeState:
    def __init__(self, args):
        self.args = args
        self.rate = args.rate
        self.dt = 1.0 / args.rate                       # fallback period
        self.channels = {}
        self.order = []
        self.lock = threading.Lock()
        self.t_window = 0.5                              # seconds shown
        self.t_offset = 0.0                              # horizontal position (s)
        self.vrange = float(1 << 12)                     # full vertical span
        # trigger
        self.trig_cid = -1
        self.trig_level = 0.0          # raw units, relative to mean when source is AC
        self.trig_rising = True
        self.trig_auto = True
        self.pretrig = 0.5
        self.dragging = False
        # status
        self.num_bytes = 0
        self.t0 = time.time()
        self.connected = False
        self.status = "discovering..."
        self.hostname = ""
        self.fps = 0.0
        self.do_autofit = False
        # device discovery / selection
        self.candidates = []           # discovered scope endpoints [(host, port, name)]
        self.requested = None          # endpoint the receive loop should hold, or None
        self.user_picked = False       # once True, auto-pick won't override the manual choice
        self.rejected = set()          # (host, port) auto-pick skips (e.g. --match mismatch)
        self.discovery = None          # ScopeDiscovery (persistent mDNS browser), set in main()
        # loaded-file ("frozen") playback
        self.frozen = False            # showing a loaded capture: ignore live data, anchor on it
        self.frozen_now = 0.0          # view anchor (latest loaded timestamp) instead of wall clock
        self.scene_dirty = False       # render thread must drop stale line graphics and rebuild
        self.rescan = threading.Event()
        # persisted settings applied to channels as they appear
        self.saved_channels = {}
        self.saved_trig_source = None

    def add_channel(self, cid, name, typ, bitlen):
        with self.lock:
            if cid in self.channels:
                return
            ch = Channel(cid, name, typ, bitlen, self.args.median)
            s = self.saved_channels.get(name)
            if s:
                ch.scale = float(s.get("scale", ch.scale))
                ch.offset = float(s.get("offset", ch.offset))
                ch.coupling = s.get("coupling", ch.coupling)
                ch.visible = bool(s.get("visible", ch.visible))
            self.channels[cid] = ch
            self.order.append(cid)
            if self.trig_cid < 0:
                self.trig_cid = cid
            if self.saved_trig_source == name:
                self.trig_cid = cid

    def channel_list(self):
        with self.lock:
            return [self.channels[c] for c in self.order]

    def clear_buffers(self):
        """Reset every channel's sample buffer (e.g. on (re)connect, to drop stale samples)."""
        with self.lock:
            for ch in self.channels.values():
                ch.clear()

    # --- device discovery / selection ------------------------------------
    def set_candidates(self, cand):
        """Replace the discovered-endpoint list (called from the discovery thread). With no manual
        choice yet, auto-connect only when there's no ambiguity: a single candidate, or a --match
        filter that already narrowed it. Multiple candidates wait for the user to pick."""
        with self.lock:
            self.candidates = cand
            if self.user_picked or self.requested is not None:
                return
            usable = [c for c in cand if (c[0], c[1]) not in self.rejected]
            if usable and (len(usable) == 1 or self.args.match):
                self.requested = usable[0]

    def list_candidates(self):
        with self.lock:
            return list(self.candidates)

    def select(self, target):
        with self.lock:
            self.user_picked = True
            self.requested = target
            if self.frozen:                 # leaving a loaded capture to go live again
                self._go_live_locked()

    def disconnect(self):
        with self.lock:
            self.user_picked = True
            self.requested = None

    def _go_live_locked(self):
        """Drop the loaded capture and let a live device repopulate the channels (caller holds lock,
        and is on a thread allowed to flag the scene — graphics removal happens in the render loop)."""
        self.frozen = False
        self.channels = {}
        self.order = []
        self.trig_cid = -1
        self.scene_dirty = True

    def request_rescan(self):
        self.rescan.set()


# ---------------------------------------------------------------------------
# wire protocol decoder (12-bit packed samples + ###ScopeHead header)
# ---------------------------------------------------------------------------
class Decoder:
    # A TCP read is not a message: the firmware's samples are a 2-byte-aligned stream with no
    # framing, so a read can split a sample, and the header can arrive split or coalesced with the
    # samples that follow it. Anything not consumable yet is carried to the next read — dropping it
    # (as this did) resyncs the stream onto the wrong byte and plots plausible wrong values.
    HEAD_SOF = b'###ScopeHead:'
    HEAD_EOF = b'###ENDHEAD\n'
    MAX_HEAD = 4096                     # bound the stash; a stream that never completes a header

    def __init__(self, state: ScopeState):
        self.s = state
        self.resid = b''

    def reset(self):
        """Drop carried bytes. Call on a new connection — residue belongs to the old stream."""
        self.resid = b''

    def _parse_header(self, body: bytes):
        for ent in body.decode('utf-8', 'replace').strip(' ,').split(','):
            if not ent:
                continue
            if ent.startswith('@host='):
                self.s.hostname = ent[6:]
                continue
            cid, rest = ent.split('$')
            name, typrepr = rest.split('=')
            self.s.add_channel(int(cid), name, typrepr[0], int(typrepr[1:]))
        print("scope header:", [(c.cid, c.name, c.typ, c.bitlen)
                                for c in self.s.channel_list()])

    def decode(self, ba: bytes, t: float):
        if self.s.frozen:                   # showing a loaded capture: ignore live samples
            return
        if self.resid:
            ba = self.resid + ba
            self.resid = b''
        if ba[:len(self.HEAD_SOF)] == self.HEAD_SOF:
            end = ba.find(self.HEAD_EOF)
            if end < 0:                     # header split across reads: wait for the rest
                if len(ba) < self.MAX_HEAD:
                    self.resid = ba
                return
            self._parse_header(ba[len(self.HEAD_SOF):end])
            ba = ba[end + len(self.HEAD_EOF):]   # samples may be coalesced after the header
        elif len(ba) < len(self.HEAD_SOF) and self.HEAD_SOF.startswith(ba):
            self.resid = ba                 # could be the start of a header
            return
        chans = self.s.channels
        n = len(ba) & ~1                # whole 2-byte samples only
        if n < len(ba):
            self.resid = ba[n:]         # odd trailing byte belongs to the next read
        if n < 2:
            return
        raw = np.frombuffer(ba, dtype=np.uint8, count=n)
        b0, b1 = raw[0::2], raw[1::2]
        ext = np.flatnonzero(b0 & 0x01)     # extended (16/32-bit) samples: stop at the first
        if ext.size:
            if ext[0] == 0:
                return
            b0, b1 = b0[:ext[0]], b1[:ext[0]]
        cid = (b0 & 0x0E) >> 1
        v = (b1.astype(np.uint16) << 4) | (b0 >> 4)     # 12-bit little-nibble-packed value
        for c in np.unique(cid):                        # one batch per channel per chunk
            ci = int(c)
            if ci in chans:
                chans[ci].add_batch(v[cid == c], t, self.s.dt)


def serve_connection(state: ScopeState, dec: Decoder, target):
    """Connect to one scope endpoint `target` = (host, port, name) and pump samples until it
    drops or the user picks another device. Returns True if the connection was held (the receive
    loop then just reconnects to whatever is requested), False if the endpoint was unusable or
    filtered out by --match (its (host, port) is marked rejected so auto-pick skips it)."""
    host, port, name = target
    if name:
        state.hostname = name
    state.status = f"connecting {host}:{port}"
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(4)
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    try:
        s.connect((host, port))
    except Exception as e:
        state.status = f"connect failed {host}:{port}: {e}"
        s.close()
        return False
    state.connected = True
    state.status = f"connected {host}:{port}"
    state.clear_buffers()               # drop stale samples from any previous connection
    dec.reset()                         # ...and any half-sample carried from the previous stream
    state.t0 = time.time()
    t_last = time.time()
    noted = False
    while True:
        if state.requested is not target:   # user picked another device / disconnected (atomic read)
            s.close()
            state.connected = False
            return True
        try:
            buf = s.recv(1024 * 8)
            if not buf:
                raise ConnectionError("closed")
            state.num_bytes += len(buf)
            t_last = time.time()
            dec.decode(buf, t_last)         # newest sample of the batch ~= now
            if not noted and state.hostname and state.discovery is not None:
                state.discovery.note_hostname(host, port, state.hostname)   # picker label + skip re-probe
                noted = True
            m = state.args.match
            if m and state.hostname and m not in state.hostname:
                state.status = f"{state.hostname} != {m}, next"
                s.close()
                state.connected = False
                with state.lock:
                    state.rejected.add((host, port))
                return False
        except Exception as e:
            if time.time() - t_last >= 8:
                state.status = f"timeout: {e}"
                s.close()
                state.connected = False
                return True
            time.sleep(0.05)


def discovery_loop(state: ScopeState):
    """Snapshot the persistent ScopeDiscovery into state every --discover-interval seconds (and
    immediately on a Rescan request). The mDNS browser runs continuously inside ScopeDiscovery, so
    a snapshot is cheap and silent — no per-sweep Zeroconf churn."""
    while True:
        cand = state.discovery.snapshot()
        if state.args.match:            # drop mDNS hosts that don't match; NAT (name=None) kept,
            cand = [c for c in cand     # its hostname is only known once the telnet probe resolves
                    if c[2] is None or state.args.match in c[2]]
        state.set_candidates(cand)
        state.rescan.wait(state.args.discover_interval)
        state.rescan.clear()


def receive_loop(state: ScopeState, dec: Decoder):
    if state.args.ip:                   # explicit target: no discovery, just (re)connect forever
        target = (state.args.ip, state.args.port, None)
        state.select(target)
        while True:
            if state.frozen:            # showing a loaded capture: don't stream
                time.sleep(0.3)
                continue
            serve_connection(state, dec, target)
            time.sleep(0.5)
    while True:                         # discovery runs in discovery_loop; here we just hold a pick
        if state.frozen:
            time.sleep(0.3)
            continue
        with state.lock:
            target = state.requested
            n = len(state.candidates)
            auto = not state.user_picked
        if target is None:
            state.status = "select a device" if n > 1 else "discovering..."
            time.sleep(0.3)
            continue
        held = serve_connection(state, dec, target)
        if not held and auto:           # auto-picked endpoint unusable; let auto-pick re-choose
            with state.lock:
                if state.requested == target and not state.user_picked:
                    state.requested = None
        time.sleep(0.3)


# ---------------------------------------------------------------------------
# trigger: most recent edge crossing `level` with at least `min_post` samples after it.
# Returns the crossing's index into `a`, or None.
# ---------------------------------------------------------------------------
def find_trigger(a, level, rising, min_post):
    if rising:
        cross = (a[:-1] < level) & (a[1:] >= level)
    else:
        cross = (a[:-1] > level) & (a[1:] <= level)
    idx = np.nonzero(cross)[0] + 1          # trigger sample index (NaN compares False)
    if idx.size == 0:
        return None
    good = idx[(a.size - 1 - idx) >= min_post]
    if good.size == 0:
        return None
    return int(good[-1])                      # most recent qualifying crossing


# ---------------------------------------------------------------------------
# control panel
# ---------------------------------------------------------------------------
class Controls(EdgeWindow):
    def __init__(self, figure, state: ScopeState, size=330, location="right"):
        super().__init__(figure=figure, size=size, location=location, title="adcscope")
        self.s = state

    def _devices(self, s):
        """Discovered-device picker. The list refreshes in the background; click one to connect,
        Disconnect to release. With a single device the receive loop auto-connects; with several it
        waits here for a pick."""
        with s.lock:
            cands = list(s.candidates)
            req = s.requested
            picked = s.user_picked
        imgui.separator()
        imgui.text(f"Devices ({len(cands)})")
        shown = list(cands)
        if req is not None and req not in shown:    # keep a NAT/manual target visible even if not advertised
            shown.append(req)
        for i, c in enumerate(shown):
            host, port, name = c
            is_req = (c == req)
            connected = s.connected and is_req
            label = (s.hostname if connected and s.hostname else (name or f"{host}:{port}"))
            mark = "> " if is_req else "  "
            imgui.push_id(i)
            clicked, _ = imgui.selectable(mark + label + ("  *" if connected else ""), is_req)
            imgui.pop_id()
            if clicked:
                s.select(c)
        if not shown:
            imgui.text_disabled("  (discovering...)")
        if imgui.small_button("Rescan"):
            s.request_rescan()
        imgui.same_line()
        if imgui.small_button("Disconnect"):
            s.disconnect()
        if req is None and not picked and len(cands) > 1:
            imgui.text_colored(imgui.ImVec4(1.0, 0.8, 0.2, 1.0), "multiple found - pick one")

    def update(self):
        s = self.s
        title = s.hostname or (f"{s.args.ip}:{s.args.port}" if s.args.ip else "")
        imgui.text(title)
        imgui.text(s.status)
        imgui.text(f"fps {s.fps:4.0f}  rx {s.num_bytes/1e3/max(1e-3, time.time()-s.t0):6.1f} kB/s")
        chans = s.channel_list()

        if not s.args.ip:
            self._devices(s)

        imgui.separator()
        imgui.text("Horizontal")
        _, s.t_window = imgui.slider_float("window (s)", s.t_window, 0.002, 60.0,
                                           "%.3f", imgui.SliderFlags_.logarithmic)
        imgui.text(f"  {s.t_window/10*1e3:.2f} ms/div")
        tch = s.channels.get(s.trig_cid)
        dtb = tch.dt_ch if (tch and tch.dt_ch > 0) else s.dt
        back = BUF_SAMPLES * dtb                          # full buffer depth in seconds
        _, s.t_offset = imgui.slider_float("offset (s)", s.t_offset, -back, s.t_window)
        imgui.same_line()
        if imgui.button("0##hoff"):
            s.t_offset = 0.0

        imgui.separator()
        imgui.text("Vertical")
        _, s.vrange = imgui.slider_float("range", s.vrange, 1.0, float(1 << 13),
                                         "%.0f", imgui.SliderFlags_.logarithmic)

        imgui.separator()
        imgui.text("Trigger  (drag plot to set level)")
        if chans:
            names = [c.name for c in chans]
            cur = next((i for i, c in enumerate(chans) if c.cid == s.trig_cid), 0)
            ch, cur = imgui.combo("source", cur, names)
            if ch:
                s.trig_cid = chans[cur].cid
        _, s.trig_auto = imgui.checkbox("auto (free-run)", s.trig_auto)
        _, s.trig_rising = imgui.checkbox("rising edge", s.trig_rising)
        _, s.trig_level = imgui.slider_float("level", s.trig_level, -float(1 << 12), float(1 << 12))
        _, s.pretrig = imgui.slider_float("pre-trigger", s.pretrig, 0.0, 1.0)

        imgui.separator()
        imgui.text("Channels")
        for c in chans:
            imgui.push_id(c.cid)
            _, c.visible = imgui.checkbox(c.name, c.visible)
            if c.line is not None:
                c.line.visible = c.visible
            imgui.same_line()
            imgui.text_colored(_rgba(c.cid), f"{c.sample_rate():.0f} Hz")
            for mode in COUPLINGS:                       # DC | AC as two small buttons
                active = c.coupling == mode
                if active:
                    imgui.push_style_color(imgui.Col_.button, imgui.ImVec4(0.20, 0.45, 0.75, 1.0))
                if imgui.small_button(mode) and not active:
                    c.coupling = mode
                    c.offset = 0.0 if mode == "AC" else (-float(c.half) if c.typ == 'u' else 0.0)
                if active:
                    imgui.pop_style_color()
                imgui.same_line()
            imgui.text("coupling")
            _, c.scale = imgui.slider_float("scale", c.scale, 0.01, 100.0,
                                            "%.3f", imgui.SliderFlags_.logarithmic)
            _, c.offset = imgui.slider_float("offset", c.offset, -float(1 << 12), float(1 << 12))
            imgui.pop_id()

        imgui.separator()
        if imgui.button("auto-fit"):
            s.do_autofit = True
        imgui.same_line()
        if imgui.button("save CSV"):
            save_csv(s)
        imgui.same_line()
        if imgui.button("save NPZ"):
            save_npz(s)
        imgui.same_line()
        if imgui.button("load..."):
            threading.Thread(target=do_load, args=(s,), daemon=True).start()
        imgui.same_line()
        if imgui.button("clear buf"):
            s.clear_buffers()


def _rgba(cid):
    import pygfx
    c = pygfx.Color(CHANNEL_COLORS[cid % len(CHANNEL_COLORS)])
    return imgui.ImVec4(c.r, c.g, c.b, 1.0)


def autofit(state: ScopeState):
    span = 1.0
    for c in state.channel_list():
        if not c.visible:
            continue
        o = c.ordered()
        finite = o[np.isfinite(o)]
        if finite.size < 2:
            continue
        c.scale = 1.0
        if c.coupling == "AC":
            c.offset = 0.0
        else:
            c.offset = -float(np.mean(finite))
        span = max(span, float(np.ptp(finite)))
    state.vrange = span * 1.4


def load_settings(state: ScopeState, path):
    try:
        with open(path) as f:
            d = yaml.safe_load(f) or {}
    except FileNotFoundError:
        return
    except Exception as e:
        print("settings load failed:", e)
        return
    v = d.get("view", {})
    state.t_window = float(v.get("window", state.t_window))
    state.t_offset = float(v.get("offset", state.t_offset))
    state.vrange = float(v.get("vrange", state.vrange))
    t = d.get("trigger", {})
    state.saved_trig_source = t.get("source")
    state.trig_auto = bool(t.get("auto", state.trig_auto))
    state.trig_rising = bool(t.get("rising", state.trig_rising))
    state.trig_level = float(t.get("level", state.trig_level))
    state.pretrig = float(t.get("pretrig", state.pretrig))
    state.saved_channels = d.get("channels", {}) or {}
    print("settings loaded from", path)


def save_settings(state: ScopeState, path):
    tch = state.channels.get(state.trig_cid)
    d = {
        "view": {"window": round(state.t_window, 6),
                 "offset": round(state.t_offset, 6),
                 "vrange": round(state.vrange, 3)},
        "trigger": {"source": tch.name if tch else state.saved_trig_source,
                    "auto": bool(state.trig_auto),
                    "rising": bool(state.trig_rising),
                    "level": round(float(state.trig_level), 3),
                    "pretrig": round(float(state.pretrig), 3)},
        "channels": {c.name: {"scale": round(float(c.scale), 4),
                              "offset": round(float(c.offset), 3),
                              "coupling": c.coupling,
                              "visible": bool(c.visible)}
                     for c in state.channel_list()},
    }
    # keep settings for channels seen in earlier sessions but absent now
    for name, s in state.saved_channels.items():
        d["channels"].setdefault(name, s)
    try:
        with open(path, "w") as f:
            yaml.safe_dump(d, f, sort_keys=False)
    except Exception as e:
        print("settings save failed:", e)


def _sanitize(s):
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in s)


def _capture_dir(state: ScopeState):
    """`<hostname>/<UTC-datetime>/` for the current capture; created on demand, with a small
    `view.json` sidecar holding each channel's scale/offset/coupling (the npz/csv store just data)."""
    import json
    d = os.path.join(_sanitize(state.hostname or "scope"),
                     time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))
    os.makedirs(d, exist_ok=True)
    view = {c.name: {"scale": round(float(c.scale), 6), "offset": round(float(c.offset), 3),
                     "coupling": c.coupling} for c in state.channel_list()}
    try:
        with open(os.path.join(d, "view.json"), "w") as f:
            json.dump(view, f)
    except OSError as e:
        print("view sidecar write failed:", e)
    return d


def _read_view(path):
    """The `view.json` sidecar (channel -> {scale,offset,coupling}) for the capture `path` is in."""
    import json
    d = path if os.path.isdir(path) else os.path.dirname(path)
    try:
        with open(os.path.join(d, "view.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _channel_samples(c):
    """The channel's finite samples oldest..newest as (values float32, timestamps float64), or None."""
    n = min(c.head, BUF_SAMPLES)
    if n < 1:
        return None
    vals, ts = c.recent(n)
    good = np.isfinite(ts) & np.isfinite(vals)
    vals, ts = vals[good], ts[good]
    return (vals, ts) if vals.size else None


def save_csv(state: ScopeState):
    """One CSV per channel under `<hostname>/<datetime>/<ch>_<SR>.csv` (time_s,value rows)."""
    import csv
    d = _capture_dir(state)
    for c in state.channel_list():
        s = _channel_samples(c)
        if s is None:
            continue
        vals, ts = s
        fn = os.path.join(d, f"{c.name}_{int(round(c.sample_rate()))}.csv")
        with open(fn, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["time_s", c.name])
            t0 = ts[0]
            for tv, vv in zip(ts - t0, vals):
                w.writerow([f"{tv:.6f}", float(vv)])
        print("written", fn)


def save_npz(state: ScopeState):
    """One compressed .npz per channel under `<hostname>/<datetime>/<ch>_<SR>.npz`, holding just the
    raw ADC samples as int16 (`v`, oldest..newest). Timing isn't stored — it's reconstructed on load
    from the `_<SR>` rate in the filename and the capture time in the directory name."""
    d = _capture_dir(state)
    for c in state.channel_list():
        s = _channel_samples(c)
        if s is None:
            continue
        vals, _ = s
        fn = os.path.join(d, f"{c.name}_{int(round(c.sample_rate()))}.npz")
        np.savez_compressed(fn, v=np.rint(vals).astype(np.int16))
        print("written", fn)


# ---------------------------------------------------------------------------
# loading saved captures back into the scope (freezes live streaming)
# ---------------------------------------------------------------------------
def _sr_from_name(base):
    """Sample rate (Hz) parsed from a `<ch>_<SR>.<ext>` filename, or None."""
    import re
    m = re.search(r"_(\d+(?:\.\d+)?)$", base.rsplit(".", 1)[0])
    return float(m.group(1)) if m else None


def _stem_name(base):
    """Channel name from `<ch>_<SR>.<ext>` (everything before the final `_<SR>`)."""
    stem = base.rsplit(".", 1)[0]
    return stem.rsplit("_", 1)[0] if _sr_from_name(base) is not None else stem


def _load_channel_file(path):
    """Parse one saved channel file into an entry dict {name, vals, ts}, or None.

    Neither format stores timing: .npz is just int16 values and .csv (when written here, value-only)
    too, so timestamps are a uniform grid from the `_<SR>` rate in the filename. A .csv that *does*
    carry a leading time column (e.g. hand-made) is honoured."""
    import csv
    base = os.path.basename(path)
    sr = _sr_from_name(base) or 1000.0
    if path.endswith(".npz"):
        z = np.load(path)
        vals = (z["v"] if "v" in z else z[z.files[0]]).astype(np.float32)
        ts = np.arange(vals.size, dtype=np.float64) / max(sr, 1e-6)
        return {"name": _stem_name(base), "vals": vals, "ts": ts}
    if path.endswith(".csv"):
        rows = list(csv.reader(open(path, newline="")))
        if not rows:
            return None
        header = rows[0]
        has_time = len(header) >= 2 and header[0].strip().lower() in ("time_s", "t", "time")
        body = rows[1:] if any(not _is_float(x) for x in header) else rows
        vals, tcol = [], []
        for r in body:
            if not r:
                continue
            if has_time and len(r) >= 2 and _is_float(r[0]) and _is_float(r[1]):
                tcol.append(float(r[0])); vals.append(float(r[1]))
            elif _is_float(r[-1]):
                vals.append(float(r[-1]))
        if not vals:
            return None
        vals = np.asarray(vals, np.float32)
        ts = (np.asarray(tcol, np.float64) if len(tcol) == len(vals)
              else np.arange(vals.size, dtype=np.float64) / max(sr, 1e-6))
        name = header[-1] if (header and not _is_float(header[-1])) else _stem_name(base)
        return {"name": name, "vals": vals, "ts": ts}
    return None


def _is_float(x):
    try:
        float(x); return True
    except (TypeError, ValueError):
        return False


def _capture_epoch(path):
    """Epoch seconds parsed from the `<...>/<UTC-datetime>/` capture directory name, or None."""
    import calendar
    d = path if os.path.isdir(path) else os.path.dirname(path)
    try:
        return float(calendar.timegm(time.strptime(os.path.basename(d.rstrip("/\\")),
                                                    "%Y%m%dT%H%M%SZ")))
    except ValueError:
        return None


def _capture_files(path):
    """Channel files for the capture `path` belongs to: every *.npz/*.csv in its directory (so
    picking any one file loads the whole capture), preferring .npz when a channel has both."""
    d = path if os.path.isdir(path) else os.path.dirname(path) or "."
    files = [os.path.join(d, f) for f in sorted(os.listdir(d)) if f.endswith((".npz", ".csv"))]
    by_stem = {}
    for f in files:
        stem = os.path.basename(f).rsplit(".", 1)[0]
        if stem not in by_stem or f.endswith(".npz"):     # prefer npz (exact int values vs csv text)
            by_stem[stem] = f
    return list(by_stem.values())


def load_capture(state: ScopeState, path):
    """Load a saved capture (the whole `<hostname>/<datetime>/` directory `path` sits in) and freeze
    the scope on it. Runs off the render thread: builds Channels here, then flags `scene_dirty` so the
    render loop swaps the line graphics."""
    entries = []
    for f in _capture_files(path):
        try:
            e = _load_channel_file(f)
            if e and e["vals"].size:
                entries.append(e)
        except Exception as ex:
            print("skip", os.path.basename(f), "-", ex)
    if not entries:
        print("load: no channels in", path)
        return
    t_end = _capture_epoch(path)
    if t_end is None:
        t_end = 0.0                       # unparseable dir name: keep timing relative
    view = _read_view(path)
    chans, order = {}, []
    for cid, e in enumerate(entries):
        ch = Channel(cid, e["name"], "u", 12, median=False)
        vals, ts = e["vals"], e["ts"]
        if vals.size > BUF_SAMPLES:
            vals, ts = vals[-BUF_SAMPLES:], ts[-BUF_SAMPLES:]
        ts = ts - ts[-1] + t_end          # end-align every channel to the capture time
        k = vals.size
        ch.ring[:k] = vals
        ch.ts[:k] = ts
        ch.head = ch.n_samples = k
        ch.t_first = float(ts[0])
        ch.ts_last = float(ts[-1])
        ch.dt_ch = float((ts[-1] - ts[0]) / (k - 1)) if k > 1 else state.dt
        vw = view.get(e["name"], {})
        ch.scale = float(vw.get("scale", ch.scale))
        ch.offset = float(vw.get("offset", ch.offset))
        ch.coupling = vw.get("coupling", ch.coupling)
        chans[cid] = ch
        order.append(cid)
    capture_dir = path if os.path.isdir(path) else os.path.dirname(path)
    with state.lock:
        state.channels = chans
        state.order = order
        state.trig_cid = order[0]
        state.frozen = True
        state.frozen_now = t_end
        state.requested = None
        state.user_picked = True
        state.scene_dirty = True
        state.status = "file: " + (os.path.basename(capture_dir.rstrip("/\\")) or capture_dir)
    print(f"loaded {len(entries)} channel(s) from {capture_dir}")


def do_load(state: ScopeState):
    """File-dialog → load_capture, in a worker thread (the native dialog blocks)."""
    path = None
    try:
        from imgui_bundle import portable_file_dialogs as pfd
        sel = pfd.open_file("Load scope capture", "",
                            ["scope captures (*.npz *.csv)", "*.npz *.csv", "All files", "*"]).result()
        path = sel[0] if sel else None
    except Exception as e:
        print("file dialog unavailable:", e)
    if path:
        load_capture(state, path)


# ---------------------------------------------------------------------------
# render
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ip", help="device IP (skip discovery entirely)")
    ap.add_argument("--port", type=int, default=24)
    ap.add_argument("-m", "--match", help="connect only to a device whose hostname contains this")
    ap.add_argument("--rate", type=float, default=2000, help="fallback sample rate Hz")
    ap.add_argument("--median", action="store_true", help="5-tap median spike filter")
    ap.add_argument("--discover-interval", type=float, default=3.0,
                    help="seconds between background device-discovery sweeps")
    args = ap.parse_args()

    state = ScopeState(args)
    load_settings(state, SETTINGS_PATH)
    atexit.register(save_settings, state, SETTINGS_PATH)
    dec = Decoder(state)
    threading.Thread(target=receive_loop, args=(state, dec), daemon=True).start()
    if not args.ip:
        state.discovery = ScopeDiscovery()
        threading.Thread(target=discovery_loop, args=(state,), daemon=True).start()

    fig = fpl.Figure(size=(1100, 650))
    sub = fig[0, 0]
    sub.axes.grids.xy.visible = True
    sub.axes.grids.xy.material.major_color = GRID_COLOR
    sub.axes.grids.xy.material.minor_color = GRID_COLOR
    sub.controller.enabled = False        # the view is driven entirely by the sliders
    sub.camera.maintain_aspect = False

    trig_line = sub.add_line(
        data=np.array([[0., 0., 0.], [1., 0., 0.]], dtype=np.float32),
        thickness=1, colors=TRIGGER_COLOR, name="__trig__")
    try:
        trig_line.world_object.material.dash_pattern = (1, 3)
    except Exception:
        pass
    trig_line.visible = False

    def level_from_event(ev):
        world = sub.map_screen_to_world((ev.x, ev.y))
        if world is None:
            return
        tch = state.channels.get(state.trig_cid)
        if tch is None:
            return
        # displayed level = (trig_level + offset)*scale  (DC removed identically for AC)
        state.trig_level = float(world[1]) / (tch.scale or 1.0) - tch.offset

    def on_down(ev):
        if getattr(ev, "button", 1) == 1:
            state.dragging = True
            level_from_event(ev)

    def on_move(ev):
        if state.dragging:
            level_from_event(ev)

    def on_up(ev):
        state.dragging = False

    fig.renderer.add_event_handler(on_down, "pointer_down")
    fig.renderer.add_event_handler(on_move, "pointer_move")
    fig.renderer.add_event_handler(on_up, "pointer_up")

    frame_t = [time.time()]
    drawn_lines = []                  # every line graphic we've added, for scene rebuilds on load

    def update():
        now = time.time()
        dt = now - frame_t[0]
        if dt > 0:
            state.fps = 0.9 * state.fps + 0.1 * (1.0 / dt)
        frame_t[0] = now

        if state.scene_dirty:         # channel set was swapped (load / go-live): rebuild all lines
            for ln in drawn_lines:
                try:
                    sub.remove_graphic(ln)
                except Exception:
                    pass
            drawn_lines.clear()
            for c in state.channel_list():
                c.line = None
                c._ndrawn = 0
            state.scene_dirty = False

        if state.do_autofit:
            autofit(state)
            state.do_autofit = False

        chans = state.channel_list()
        for c in chans:
            if c.line is None:
                col = CHANNEL_COLORS[c.cid % len(CHANNEL_COLORS)]
                data = np.full((BUF_SAMPLES, 3), np.nan, dtype=np.float32)
                data[:, 2] = 0.0
                c.line = sub.add_line(data=data, thickness=1, colors=col, name=c.name)
                c.line.visible = c.visible
                c._ndrawn = 0
                drawn_lines.append(c.line)

        tw = state.t_window
        tch = state.channels.get(state.trig_cid)

        # anchor on the loaded capture's end when frozen, else on the wall clock
        t_clock = state.frozen_now if state.frozen else now

        # trigger search on the source channel (raw units; AC -> relative to visible mean)
        triggered = False
        t_anchor = t_clock
        if tch is not None and min(tch.head, BUF_SAMPLES) >= 2:
            dtc = tch.dt_est(state.dt)
            n_win = int(np.clip(tw / dtc, 16, min(tch.head, BUF_SAMPLES)))
            vals, ts = tch.recent(int(np.clip(3 * n_win, 16, min(tch.head, BUF_SAMPLES))))
            dc = np.nanmean(vals[-n_win:])
            if not np.isfinite(dc):
                dc = 0.0
            level = state.trig_level + (dc if tch.coupling == "AC" else 0.0)
            min_post = int((1.0 - state.pretrig) * n_win)
            c = find_trigger(vals, level, state.trig_rising, min_post)
            if c is not None:
                t_anchor = float(ts[c])                  # trigger event timestamp
                triggered = True

        if not triggered and not state.trig_auto:
            return                                       # normal mode: hold last frame
        if triggered:
            x_left, x_right = -state.pretrig * tw, (1.0 - state.pretrig) * tw
        else:
            x_left, x_right = -tw, 0.0

        t_anchor += state.t_offset      # horizontal position: pan the view in time

        for c in chans:
            if c.line is None or not c.visible:
                continue
            win = c.window(t_anchor, x_left, x_right, state.dt)
            if win is None:
                if c._ndrawn:
                    c.line.data[:c._ndrawn, 0] = np.nan
                    c._ndrawn = 0
                continue
            xs, ys = win
            if c.coupling == "AC":
                m = np.nanmean(ys)
                ys = ys - (m if np.isfinite(m) else 0.0)
            k = xs.size
            c.line.data[:k, 0] = xs
            c.line.data[:k, 1] = (ys + c.offset) * c.scale     # offset first, then scale
            if k < c._ndrawn:
                c.line.data[k:c._ndrawn, 0] = np.nan
            c._ndrawn = k

        # trigger level line (displayed level is independent of coupling DC removal)
        if tch is not None:
            disp = (state.trig_level + tch.offset) * tch.scale
            trig_line.data[:, 0] = [x_left, x_right]
            trig_line.data[:, 1] = disp
            trig_line.visible = True
        else:
            trig_line.visible = False

        cam = sub.camera
        cam.width = max(x_right - x_left, 1e-9)
        cam.height = state.vrange
        cam.local.position = ((x_left + x_right) / 2.0, 0.0, cam.local.position[2])

    def _on_signal(signum, frame):
        save_settings(state, SETTINGS_PATH)
        os._exit(0)
    for _sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(_sig, _on_signal)
        except Exception:
            pass

    sub.add_animations(update)
    fig.add_gui(Controls(fig, state))
    fig.show(maintain_aspect=False)
    fpl.loop.run()
    save_settings(state, SETTINGS_PATH)


if __name__ == "__main__":
    main()
