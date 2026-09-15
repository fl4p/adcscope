*this document is an LLM generated placeholder*

# adcscope

A soft oscilloscope for microcontroller ADCs. A device streams channel-tagged 12-bit samples over
TCP; this client discovers devices, decodes the stream, triggers and plots it like a real scope,
and saves captures.

Extracted from [fugu-mppt-firmware](https://github.com/fl4p/fugu-mppt-firmware), where it grew as
`etc/scope_client`. Nothing here is specific to that firmware or to an ESP32 — the wire protocol is
[PROTOCOL.md](PROTOCOL.md), and any device that speaks it works.

## Why not an existing scope app

Surveyed before writing this — see [doc/prior-art.md](doc/prior-art.md). ngscopeclient is
GPU-accelerated and professional but could not be made to software-trigger on this waveform; scoppy
is Android and effectively closed; Analog's scopy/libiio wants an IIO device. The gap was a client
for a microcontroller that streams its *own* ADC over a socket, with no scope hardware in the loop.

## Install

```bash
pip install -r requirements.txt
python adcscope.py
```

`fastplotlib`/`pygfx` (WGPU) drive the display; `zeroconf` does discovery. The legacy matplotlib
reference client, `scope-client.py`, needs only numpy/pandas/matplotlib/scipy and is useful when the GPU
stack will not install.

## Use

```bash
python adcscope.py                        # discover via mDNS, pick in the UI
python adcscope.py -m fry                 # auto-pick a discovered device by hostname substring
python adcscope.py --ip 192.168.4.2 [--port 24]
```

Sliders set the time window and vertical range; each channel has its own gain, offset and DC/AC
coupling; the edge trigger has auto/normal mode, adjustable pre-trigger and a draggable level line.
Discovery runs continuously in the background, so devices can be switched live.

Captures write one file per channel under `<hostname>/<datetime>/<ch>_<SR>.{npz,csv}` plus a
`view.json` sidecar. The npz holds only raw int16 samples — timing is reconstructed on load from
the sample rate in the filename and the capture time in the directory name.

## Layout

| path | |
|---|---|
| `adcscope.py` | the client: decoder, ring buffers, trigger, GPU plot, capture I/O |
| `discover.py` | mDNS `_scope._tcp` discovery, plus hooks for deployment-specific endpoints |
| `scope-client.py` | legacy matplotlib reference client |
| `filt.py`, `plot_capture.py` | offline filtering and capture plotting |
| `contrib/fugu_nat.py` | example discovery adapter: NAT-forwarded boards named from a telnet banner |
| `anf.py`, `ewm.py` | adaptive/EWMA filters the legacy client constructs per channel |
| `tests/` | wire decoder, discovery hooks, capture round-trip, device selection; plain scripts, no pytest |

```bash
for t in tests/test_*.py; do python "$t" || break; done   # 42 tests, no network needed
```

## Discovery hooks

mDNS is the only discovery mechanism in the protocol. Anything else — a board behind a NAT router,
a fixed lab address, a tunnel — registers itself:

```python
from discover import register_endpoint_source, register_name_resolver

@register_endpoint_source
def my_endpoints():            # -> [(host, port)]
    return [("192.168.1.231", 243)]

@register_name_resolver
def my_names(host, port):      # -> hostname | None
    return "bench-1"
```

`contrib/fugu_nat.py` is a working implementation of both.

## Device side

The firmware half is still `src/tele/scope.h` in fugu-mppt-firmware, not vendored here yet: it is
small, it still carries firmware couplings (a FreeRTOS task notification, an arduino-esp32
`WiFi.setTxPower()` call, a build-flag global), and it is still changing. PROTOCOL.md is the
contract in the meantime.

Known gaps documented there rather than fixed: the 8-channel ceiling aliases silently, dropped
samples are undetectable by the client, and the declared 16/32-bit sample forms are unimplemented
on both sides — with a struct-packing trap waiting for whoever implements them.
