*this document is an LLM generated placeholder*

# Implement a scope client

Use `scope-client.py` as a reference.
It connects to a MCU and receives real-time ADC data and plots it.
The drawing performance is a bit low, and the triggering doesn't work
properly. Implement in python using fastplotlib with GPU acceleration.
(Just a note: egui to create web apps and `ngscopeclient` are alternatives)

# existing scope client reference

use scope-client.py only to understand how to connect and how to read data samples.
for the actual scope display start from scratch and imitate a real oscilloscope.

# display

* x-Axis: time (horizontal)
    * a sliding window that moves with time
    * up to 60s lookback
    * controls: slider for time window and offset
* y-Axis: ADC value (unitless)
    * slider for the vertical range (visible y span)
* the scope has grid lines with the color #444.
* put colors in constants at the beginning of the file.
* there is a 200k sample buffer.

# Trigger

the trigger should replicate the trigger of an ordinary oscilloscope.
the trigger level can be adjusted by dragging over the scope with the mouse.
show the trigger level as a thin dotted line.
trigger supports DC and AC coupling, follows channel coupling.
the triggering channel can be selected.
there is auto vs normal mode (auto = free-run if no edge detected).
implement rising and falling edge triggering.
a pre-trigger slider sets the horizontal position of the trigger point (0..1).

# sample timestamping

* samples arrive in batches, at various (per-channel) sampling rates
* estimate each channel's sample period from the batch arrival times and sample counts (EWMA)
* on each batch, assign a wall-clock timestamp to every sample and never change it afterwards, so
  the displayed history stays put and the trace doesn't jump when a new batch arrives
* the newest sample's timestamp is eased toward the arrival time with a gentle clock-recovery loop
  (rather than snapped to "now"), and the rest of the batch is spaced back by the estimated period —
  this tracks the true rate without making the right edge snap at block boundaries

# channel controls

channel coupling mode DC (normal), AC (removes DC component averaged over the visible interval), can be selected with 2
small buttons.
each channel can have its own adjustable offset and scale, both adjustable through sliders.
first apply the offset, then scale.
each channel has a visibility toggle (show/hide).
an "auto-fit" button sets every channel's scale/offset and the vertical range to fit the signals.

# misc

* display the hostname of the device
* make all settings (view, trigger, channels) persist in a yaml file
* a "save CSV" button exports each channel's buffer to a csv file
* status readout: connection state, frame rate, throughput, and per-channel measured sample rate
