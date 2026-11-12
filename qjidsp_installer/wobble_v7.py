# -*- coding: utf-8 -*-
"""
wobble_v7.py -- "sound-field wobble and volume-link" for v7 Harmonic Hall mode

Moves, little by little during playback, the faders of the Volume filters placed on the
wet path (the hall reverb) of spatial_final.yml (wet_fader_L = Aux1 / wet_fader_R = Aux2).

  - Wobble      : raises and lowers the amount of reverb on the left and right, very slowly
                  (default 60 s period), with the two sides out of phase
  - Volume-link : the louder the playback level (RMS), the deeper the wobble and the more reverb
  - [e] settings: follows the control file of qji_field_ctl.py (reverb, wobble depth / period,
                  volume-link)
  - v7 and v8 share the same mechanism. The difference in strength (how much reverb is added)
    is decided by PROFILES in qji_field_ctl.py

A fader at 0 dB is a straight pass-through. Whether this script has not started yet or has
stopped, the sound does not change (fail-safe).
Note: on the CamillaDSP side, Aux faders only take effect when the YAML contains Volume filters.
"""
import math
import os
import signal
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from camilladsp import CamillaClient

try:
    import qji_field_ctl
except Exception:            # even without qji_field_ctl.py the wobble alone still runs with default values
    qji_field_ctl = None

HOST = "127.0.0.1"
PORT = int(os.environ.get("QJI_CDSP_PORT", "1234"))
FADER_L, FADER_R = 1, 2          # Aux1 = left reverb, Aux2 = right reverb

UPDATE_INTERVAL = 0.20           # seconds
BASE_PERIOD = 60.0               # base wobble period (s). Changed by the [e] period multiplier
DEPTH_MIN, DEPTH_MAX = 0.1, 1.0  # wobble depth (+/- dB). Moves between these according to volume (x depth multiplier)

QUIET_DB, LOUD_DB = -35.0, -15.0 # playback level (RMS) in this range is converted to a 0-1 intensity
SMOOTHING_ALPHA = 0.08

FADER_MIN, FADER_MAX = -12.0, 6.0   # absolute safety range (the YAML-side limit is also +6 dB)
MIN_STEP_DB = 0.01                  # changes smaller than this are not sent

DEFAULT_CTL = {"wet_offset_db": 0.0, "depth_scale": 1.0, "period_scale": 1.0,
               "loud_link": False, "loud_boost_db": 1.5}


def _ctl():
    if qji_field_ctl is None:
        return DEFAULT_CTL
    try:
        return qji_field_ctl.read_ctl()
    except Exception:
        return DEFAULT_CTL


def _clamp(v):
    return max(FADER_MIN, min(FADER_MAX, v))


class _Quit(Exception):
    pass


def _on_term(*_a):
    raise _Quit()


def main():
    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGINT, _on_term)
    cdsp = CamillaClient(HOST, PORT)
    print("v7 Harmonic Hall mode: wobble / volume-link started", flush=True)

    try:
        while True:
            try:
                cdsp.connect()
                print("Connected", flush=True)
                phase = 0.0
                intensity = 0.3
                last_l = last_r = None
                t_prev = time.monotonic()

                while True:
                    now = time.monotonic()
                    dt, t_prev = now - t_prev, now
                    ctl = _ctl()

                    # accumulate the phase so it does not jump when the period multiplier changes
                    period = BASE_PERIOD * float(ctl.get("period_scale", 1.0))
                    phase += 2.0 * math.pi * dt / max(period, 1.0)

                    try:
                        rms = cdsp.levels.playback_rms()
                        avg_db = sum(rms) / len(rms) if rms else QUIET_DB
                    except Exception:
                        avg_db = QUIET_DB
                    raw = max(0.0, min(1.0, (avg_db - QUIET_DB) / (LOUD_DB - QUIET_DB)))
                    intensity += SMOOTHING_ALPHA * (raw - intensity)

                    depth = (DEPTH_MIN + intensity * (DEPTH_MAX - DEPTH_MIN)) * float(ctl.get("depth_scale", 1.0))
                    offset = float(ctl.get("wet_offset_db", 0.0))
                    if ctl.get("loud_link"):
                        offset += float(ctl.get("loud_boost_db", 1.5)) * intensity

                    wl = _clamp(offset + depth * math.sin(phase))
                    wr = _clamp(offset + depth * math.sin(phase + math.pi / 2))
                    if last_l is None or abs(wl - last_l) >= MIN_STEP_DB:
                        cdsp.volume.set_volume(FADER_L, wl)
                        last_l = wl
                    if last_r is None or abs(wr - last_r) >= MIN_STEP_DB:
                        cdsp.volume.set_volume(FADER_R, wr)
                        last_r = wr

                    time.sleep(UPDATE_INTERVAL)

            except _Quit:
                raise
            except Exception as e:
                print(f"Connection error: {e}", flush=True)
                time.sleep(3)
    except _Quit:
        # on exit, return the faders to 0 dB (pass-through). Failure does not matter
        try:
            cdsp.volume.set_volume(FADER_L, 0.0)
            cdsp.volume.set_volume(FADER_R, 0.0)
        except Exception:
            pass
        sys.exit(0)


if __name__ == "__main__":
    main()
