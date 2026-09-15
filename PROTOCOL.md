*this document is an LLM generated placeholder*

# adcscope wire protocol, v1

What a device actually sends today, as implemented in `Scope` (fugu-mppt-firmware
`src/tele/scope.h`) and decoded by `adcscope.py`. Where the firmware's own comments disagree with
its code, this file follows the code and says so.

The format has one goal: get a 12-bit ADC sample onto a socket in 2 bytes, from a real-time loop,
without allocating. Everything absent from it — timestamps, framing, sequence numbers, checksums,
compression — is absent on purpose, because the transport is TCP on a LAN and the cost was judged
against a per-sample budget.

## Transport

TCP, default port **24**, `TCP_NODELAY`, **one client at a time**. A second connection is not
refused; it simply is not accepted until the first drops.

Devices advertise over mDNS as `_scope._tcp`. The advert carries the address, port and hostname,
and is the only discovery mechanism that is part of the protocol.

## Session

On accept, the server sends one ASCII header, then streams samples until the socket closes. There
is no other control traffic in either direction — the client never sends anything.

### Header

```
###ScopeHead:[@host=<hostname>,]<cid>$<name>=<typ><bits>,[<cid>$<name>=<typ><bits>,...]###ENDHEAD\n
```

* `@host=` is optional and appears first when present.
* One entry per channel, each terminated by `,` — **including the last**, so a trailing comma sits
  immediately before `###ENDHEAD`. Split on `,` and discard empty fields.
* `<cid>` is the decimal channel id used in the sample stream.
* `<typ>` is one character: `u` unsigned, `i` signed, `f` float. `<bits>` is decimal.
* In practice every channel is declared `u12`, because 12-bit is the only sample form implemented
  (below). A producer that rescales into 12 bits declares `u12` regardless of the physical
  quantity — the type describes the *wire value*, not the sensor.

The header is sent once, in a single `write()`. It is not repeated, so a client that misses it
cannot recover without reconnecting. It is not length-prefixed either; clients recognise it by the
`###ScopeHead:` prefix and `###ENDHEAD\n` terminator.

### Sample stream

A continuous, unframed sequence of samples. There are no record boundaries above the sample, and
no timestamps — a client reconstructs the time axis from arrival time and an estimated per-channel
period.

**12-bit sample — 2 bytes. The only form any implementation emits.**

```
byte 0:  bit 0     = 0          (form selector: 0 = 12-bit)
         bits 1-3  = channel id (0-7)
         bits 4-7  = value bits 0-3
byte 1:  bits 0-7  = value bits 4-11
```

Decode:

```python
form = b0 & 0x01            # must be 0
cid  = (b0 & 0x0E) >> 1
val  = (b1 << 4) | (b0 >> 4)
```

Verified by round-trip against the firmware's `Data12Ch4` bitfield struct: `cid=5, val=0xABC`
serialises to `CA AB`.

## Not implemented

`scope.h` declares two wider forms, `Data16Ch4` and `Data32Ch4`, selected by `byte0 bit 0 == 1`.
**Neither is implemented on either side**, and a client should treat a set bit-0 as end of usable
data rather than as a wider sample:

* The encoder has no path to them. `addSample16()` is commented out; `addSample12()` is the only
  writer.
* The firmware's own `ScopeDecoder` throws `range_error("not impl")` on the 32-bit form, and its
  16-bit branch reads from the residual buffer `buf` rather than the current position `start` — it
  has never run.
* `adcscope.py` stops at the first sample with bit 0 set and discards the rest of the chunk.

Two traps for anyone implementing them later:

* **`sizeof(Data16Ch4)` is 4, not 3.** The `// 3bytes` comment is wrong: a trailing `reserved:1`
  bitfield after `data1`/`data2` opens a fourth byte. Measured, not inferred. An encoder written to
  the comment and a decoder written to the struct would disagree by one byte per sample and
  desynchronise the stream permanently, since there is no framing to resync on.
* `sizeof(Data32Ch4)` is 5, which does match its comment.

## Limits worth knowing

* **8 channels, hard.** The channel id is 3 bits. `Scope::addChannel()` hands out ids by
  incrementing a counter with no cap, and `addSample12()` writes that id into a 3-bit field, so the
  9th channel silently aliases onto channel 0 — no error, plausible-looking data on the wrong
  trace. fugu registers up to 7 (five sensors, plus `ucTemp` and a filtered `vout_filt`), i.e. one
  short of the limit.
* **Samples are dropped, not buffered, under backpressure.** The device holds two 2 KB buffers.
  The producer fills one while the network task writes the other; if it fills its buffer and the
  other is still unsent, the sample is discarded and a single `buffer over-flow, dropping sample`
  warning is logged until the condition clears. A gap in the stream is therefore invisible to the
  client — there is no sequence number to detect it, and it appears as a time-axis stretch.
* **No integrity check.** TCP's checksum is the only one. There is no CRC, and no way to
  distinguish a truncated sample at the end of a chunk from a valid one, which is why a client
  must buffer an odd trailing byte across reads.
* **The value is not physical.** Producers rescale into 12 bits with whatever factor suits them
  (`vout_filt` is `vout / 60.0 * 2000.0`; the INA226 current channel is `abs(raw) / 3`). The
  protocol carries no scale factor, so converting a trace back to volts or amps requires knowing
  the producer. This is the largest thing the format does not do.

## Version

There is no version field on the wire. "v1" names the format described here; a future revision
would have to be negotiated by a new header key or a distinct mDNS service name.
