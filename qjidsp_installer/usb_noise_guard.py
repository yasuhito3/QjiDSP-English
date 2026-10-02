# -*- coding: utf-8 -*-
"""
usb_noise_guard.py - USB output digital-noise mitigation module

[Purpose]
Addresses the causes of "digital noise" on USB-connected DACs/audio
interfaces (clicks/pops, jitter-related degradation, rare dropouts) that
can actually be mitigated from the software side.

Causes addressed (independent of each other, each can be toggled on its own):
  1. USB autosuspend (a power-saving feature that cuts power to an idle
     USB device) -> the latency on wake-up can cause clicks/brief dropouts.
     Disabling it tends to lower the noise floor during silence.
  2. Low scheduling priority for the playback process (camilladsp/wobble,
     etc.) -> other processes taking the CPU can cause buffer underruns,
     producing tiny timing fluctuations (jitter) in sample delivery.
     Granting realtime priority makes imaging more precise, but can also
     reduce the "thickness"/"forwardness" that the jitter-induced blur
     was contributing.

Causes this module cannot address (hardware/physical-layer):
  - USB cable/hub quality, power-supply noise, ground loops
  - EMI from a USB 3.0 controller leaking into USB 2.0 audio
  These are physical-layer issues outside the scope of software, so this
  module only surfaces a diagnostic note about them.

[Usage]
  from usb_noise_guard import optimize_usb_audio_output
  optimize_usb_audio_output(card_num, extra_pids=[cdsp_proc.pid])

  Live A/B comparison (isolating a single cause):
    toggle_autosuspend_guard()  # toggle the autosuspend mitigation only
    toggle_rtprio_guard()       # toggle the RT-priority mitigation only
    toggle_usb_audio_output()   # toggle both together

One-time setup (run once, requires root):
  Run install_usb_audio_optimize.sh beforehand.
  (It creates a udev rule to disable USB autosuspend and grants rtprio
   permissions. The shell script itself isn't Qji-specific — it's an
   OS-level setting that benefits USB audio on Linux in general, so it
   helps other players too.)
  Even if it hasn't been run, this module never raises — it just prints
  a warning and playback continues.
"""

import os
import re
import subprocess


def card_num_from_alsa_device(device_str):
    """Extract the card number from an ALSA device string such as
    'hw:2,0' or 'plughw:2,0'. Returns None for formats that don't use
    "hw:" (e.g. BlueALSA)."""
    if not device_str:
        return None
    m = re.search(r'hw:(\d+)', device_str)
    return m.group(1) if m else None


def _find_usb_device_syspath(card_num):
    """Resolve the sysfs path of the USB device corresponding to an ALSA
    card number. /sys/class/sound/cardN/device often points at the USB
    "interface" directory, so walk up the parents until an idVendor file
    is found. Returns None for a card that isn't USB-connected (onboard,
    HDMI, etc.).
    """
    try:
        link_path = f'/sys/class/sound/card{card_num}/device'
        real_path = os.path.realpath(link_path)
        cur = real_path
        for _ in range(6):  # interface -> device body is only a few levels; cap it
            if os.path.exists(os.path.join(cur, 'idVendor')):
                return cur
            parent = os.path.dirname(cur)
            if parent == cur:
                break
            cur = parent
    except Exception:
        pass
    return None


def _read_sysfs(path):
    try:
        with open(path, 'r') as f:
            return f.read().strip()
    except Exception:
        return None


def _write_sysfs(path, value):
    try:
        with open(path, 'w') as f:
            f.write(value)
        return True
    except PermissionError:
        return False
    except Exception:
        return False


def check_autosuspend(dev_syspath):
    """Check the current USB autosuspend setting for a device.
    Returns: (is_disabled: bool, control_value: str or None)
    """
    control = _read_sysfs(os.path.join(dev_syspath, 'power', 'control'))
    return (control == 'on'), control


def fix_autosuspend(dev_syspath):
    """Disable autosuspend (returns False if there's no write permission).
    If the udev rule is already applied, the value is typically already
    'on', so the write here is a harmless no-op.
    """
    ok_control = _write_sysfs(os.path.join(dev_syspath, 'power', 'control'), 'on')
    # The autosuspend delay value. Not present on every kernel, so a
    # failure here can be safely ignored.
    _write_sysfs(os.path.join(dev_syspath, 'power', 'autosuspend'), '-1')
    return ok_control


def get_rtprio_limit():
    """Return the realtime-priority limit granted to the current process.
    0 likely means the `audio` group hasn't been granted rtprio
    (limits.d) permission yet.
    """
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_RTPRIO)
        return soft, hard
    except Exception:
        return 0, 0


def apply_realtime_priority(pid, priority=40):
    """Grant FIFO realtime scheduling priority to the given PID.
    Fails silently on insufficient permission, leaving playback at normal
    priority (audio still plays, but the jitter/click risk under CPU
    contention remains).
    """
    if not pid:
        return False
    try:
        result = subprocess.run(
            ['chrt', '-f', '-p', str(priority), str(pid)],
            capture_output=True, text=True, timeout=3
        )
        return result.returncode == 0
    except Exception:
        return False


def clear_realtime_priority(pid):
    """Return the given PID's scheduling to normal priority (SCHED_OTHER)."""
    if not pid:
        return False
    try:
        result = subprocess.run(
            ['chrt', '-o', '-p', '0', str(pid)],
            capture_output=True, text=True, timeout=3
        )
        return result.returncode == 0
    except Exception:
        return False


# State remembering what was last applied. The toggle functions read this
# to flip things on/off (so the caller doesn't need to pass the card
# number or PIDs again each time). The autosuspend and RT-priority
# mitigations each get their own "enabled" flag so they can be toggled
# independently.
_LAST_STATE = {
    'card_num': None, 'dev_syspath': None, 'extra_pids': [], 'priority': 40,
    'autosuspend_enabled': False, 'rtprio_enabled': False,
}


def optimize_usb_audio_output(card_num, extra_pids=None, priority=40, verbose=True):
    """Apply the USB output digital-noise mitigations and print a
    diagnostic report.

    Args:
      card_num  : the ALSA card number for the DSP output (string "2" or int 2)
      extra_pids: list of process IDs to grant realtime priority to
                  (camilladsp, aplay, the wobble script, etc.)
      priority  : priority passed to `chrt -f` (1-99); 40 is a common
                  choice for audio playback
      verbose   : if True, print the diagnostic report to stdout

    This function never raises (keeping playback running is the top
    priority).
    """
    global _LAST_STATE
    report = {'usb_device_found': False, 'autosuspend_disabled': False,
              'rtprio_available': False, 'rtprio_applied': []}
    dev_syspath = None
    try:
        card_num_str = str(card_num)
        dev_syspath = _find_usb_device_syspath(card_num_str)

        if verbose:
            print('\n' + '=' * 60)
            print('🔌 USB output digital-noise mitigation check')
            print('=' * 60)

        if dev_syspath:
            report['usb_device_found'] = True
            is_disabled, control_value = check_autosuspend(dev_syspath)
            if not is_disabled:
                fix_autosuspend(dev_syspath)
                is_disabled, control_value = check_autosuspend(dev_syspath)
            report['autosuspend_disabled'] = is_disabled
            report['runtime_toggle_writable'] = os.access(
                os.path.join(dev_syspath, 'power', 'control'), os.W_OK)
            if verbose:
                if is_disabled:
                    print('✅ USB autosuspend: disabled (prevents power-cycle clicks)')
                else:
                    print(f'⚠️ USB autosuspend: still enabled (power/control="{control_value}")')
                    print('   -> please run install_usb_audio_optimize.sh with sudo once')
                if report['runtime_toggle_writable']:
                    print('✅ Live ON/OFF toggle (k): available')
                else:
                    print('⚠️ Live ON/OFF toggle (k): unavailable (write permission not delegated)')
                    print('   -> re-run the latest install_usb_audio_optimize.sh with sudo')
        else:
            if verbose:
                print('ℹ️ This card was not detected as a USB device (possibly onboard, etc.)')

        soft_rt, _hard_rt = get_rtprio_limit()
        report['rtprio_available'] = soft_rt > 0
        if verbose:
            if soft_rt > 0:
                print(f'✅ Realtime priority: available (limit {soft_rt})')
            else:
                print('⚠️ Realtime priority: not permitted (less resistant to buffer underruns)')
                print('   -> run install_usb_audio_optimize.sh with sudo once, then log back in')

        if extra_pids:
            for pid in extra_pids:
                if apply_realtime_priority(pid, priority=priority):
                    report['rtprio_applied'].append(pid)
            if verbose:
                if report['rtprio_applied']:
                    print(f'✅ Realtime priority granted: PID {report["rtprio_applied"]} (priority {priority})')
                elif soft_rt > 0:
                    print('⚠️ Failed to grant realtime priority (chrt not installed, etc.)')

        if verbose:
            print('=' * 60 + '\n')

        # Remember what was applied, for live A/B toggling via the
        # toggle_* functions below.
        _LAST_STATE = {
            'card_num': card_num_str,
            'dev_syspath': dev_syspath,
            'extra_pids': list(extra_pids) if extra_pids else [],
            'priority': priority,
            'autosuspend_enabled': report['autosuspend_disabled'],
            'rtprio_enabled': bool(report['rtprio_applied']),
        }
    except Exception as _e:
        if verbose:
            print(f'⚠️ Error during the USB noise-guard check (playback continues): {_e}')
    return report


def list_usb_audio_cards():
    """List the connected ALSA cards that are USB audio devices.
    Each element: {'card_num': str, 'name': str, 'vendor': str, 'product': str,
                    'dev_syspath': str, 'autosuspend_disabled': bool}
    """
    results = []
    try:
        base = '/sys/class/sound'
        if not os.path.isdir(base):
            return results
        for entry in sorted(os.listdir(base)):
            m = re.match(r'^card(\d+)$', entry)
            if not m:
                continue
            card_num = m.group(1)
            dev_syspath = _find_usb_device_syspath(card_num)
            if not dev_syspath:
                continue
            name = _read_sysfs(os.path.join(dev_syspath, 'product')) or 'Unknown'
            vendor = _read_sysfs(os.path.join(dev_syspath, 'idVendor')) or '????'
            product = _read_sysfs(os.path.join(dev_syspath, 'idProduct')) or '????'
            is_disabled, _control = check_autosuspend(dev_syspath)
            results.append({
                'card_num': card_num, 'name': name, 'vendor': vendor,
                'product': product, 'dev_syspath': dev_syspath,
                'autosuspend_disabled': is_disabled,
            })
    except Exception:
        pass
    return results


def card_num_from_arg(arg):
    """Normalize a CLI argument of the form 'hw:2,0' / 'plughw:2,0' / '2'
    to a card-number string."""
    if arg is None:
        return None
    arg = str(arg).strip()
    if arg.isdigit():
        return arg
    return card_num_from_alsa_device(arg)


def _pid_is_realtime(pid):
    """Check whether the given PID is currently on realtime scheduling
    (FIFO/RR). Returns None if the process no longer exists."""
    try:
        policy = os.sched_getscheduler(pid)
        return policy in (os.SCHED_FIFO, os.SCHED_RR)
    except (ProcessLookupError, OSError, AttributeError):
        return None


def get_guard_status():
    """Read the current mitigation state from the *actual system state*,
    not from internal flags.

    Returns: {'autosuspend': True/False/None, 'rtprio': True/False/None}
      autosuspend: True if power/control is "on" (autosuspend disabled =
                   mitigation ON). None if no USB device was detected.
      rtprio     : True if every registered, still-alive process is on
                   realtime priority; False if any one of them isn't.
                   None if no registered process is still alive.
    """
    st = _LAST_STATE
    result = {'autosuspend': None, 'rtprio': None}
    dev_syspath = st.get('dev_syspath')
    if dev_syspath:
        is_disabled, control = check_autosuspend(dev_syspath)
        result['autosuspend'] = is_disabled if control is not None else None
    flags = [_pid_is_realtime(pid) for pid in (st.get('extra_pids') or [])]
    alive = [f for f in flags if f is not None]
    if alive:
        result['rtprio'] = all(alive)
    return result


def _sync_state():
    """Sync the internal flags to the actual state (prevents flag drift)."""
    status = get_guard_status()
    if status['autosuspend'] is not None:
        _LAST_STATE['autosuspend_enabled'] = status['autosuspend']
    if status['rtprio'] is not None:
        _LAST_STATE['rtprio_enabled'] = status['rtprio']
    return status


def format_guard_status():
    """Return the current state as a single-line string (for display in
    Qji's UI, etc.).
    Example: 🔌 Status: [k] autosuspend guard=ON 🟢 | [j] RT priority=OFF ⚪"""
    status = _sync_state()

    def _mark(v):
        if v is True:
            return 'ON 🟢'
        if v is False:
            return 'OFF ⚪'
        return 'unknown ➖'

    return (f"🔌 Status: [k] autosuspend guard={_mark(status['autosuspend'])} | "
            f"[j] RT priority={_mark(status['rtprio'])}")


_PERM_HINT = ('   -> write permission has not been delegated. Please re-run the\n'
              '     latest install_usb_audio_optimize.sh with sudo (no need to log\n'
              '     back in; unplug/replug the DAC if it still doesn\'t take effect)')


def toggle_autosuspend_guard(verbose=True):
    """Toggle just the USB autosuspend mitigation (leaves RT priority alone).

    Reads the actual sysfs value (power/control) rather than an internal
    flag. On a failed write, the state is left unchanged and None is
    returned (so the caller doesn't mistakenly report a state change).

    Returns: the state after toggling (True=ON / False=OFF).
             None if no device was detected or the write failed.
    """
    global _LAST_STATE
    _sync_state()
    st = _LAST_STATE
    dev_syspath = st.get('dev_syspath')
    if not dev_syspath:
        if verbose:
            print('⚠️ No USB device detected — cannot toggle the autosuspend mitigation')
        return None
    control_path = os.path.join(dev_syspath, 'power', 'control')
    is_disabled, _control = check_autosuspend(dev_syspath)
    if is_disabled:
        ok = _write_sysfs(control_path, 'auto')
        if ok:
            st['autosuspend_enabled'] = False
            if verbose:
                print('🔙 Autosuspend mitigation: OFF (reverted power/control to "auto")')
            return False
        if verbose:
            print('⚠️ Could not turn it OFF (no write permission). It remains ON')
            print(_PERM_HINT)
        st['autosuspend_enabled'] = True
        return None
    else:
        ok = fix_autosuspend(dev_syspath)
        if ok:
            st['autosuspend_enabled'] = True
            if verbose:
                print('✅ Autosuspend mitigation: ON (prevents power-cycle clicks)')
            return True
        if verbose:
            print('⚠️ Could not turn it ON (no write permission). It remains OFF')
            print(_PERM_HINT)
        st['autosuspend_enabled'] = False
        return None


def toggle_rtprio_guard(verbose=True):
    """Toggle just the realtime-priority mitigation (leaves autosuspend alone).

    Reads each process's actual scheduling state rather than an internal flag.
    Returns: the state after toggling (True=ON / False=OFF).
             None if there's no target process or the toggle failed.
    """
    global _LAST_STATE
    status = _sync_state()
    st = _LAST_STATE
    pids = [pid for pid in (st.get('extra_pids') or []) if _pid_is_realtime(pid) is not None]
    if not pids:
        if verbose:
            print('⚠️ No target process found — cannot toggle realtime priority')
        return None
    if status['rtprio']:
        for pid in pids:
            clear_realtime_priority(pid)
        after = _sync_state()['rtprio']
        if after is False:
            if verbose:
                print(f'🔙 RT-priority mitigation: OFF (reverted to normal priority, PID {pids})')
            return False
        if verbose:
            print('⚠️ Could not turn it OFF. It remains ON')
        return None
    else:
        for pid in pids:
            apply_realtime_priority(pid, priority=st.get('priority', 40))
        after = _sync_state()['rtprio']
        if after is True:
            if verbose:
                print(f'✅ RT-priority mitigation: ON (granted priority {st.get("priority", 40)} to PID {pids})')
            return True
        if verbose:
            print('⚠️ Could not turn it ON (chrt not installed, or insufficient permission). It remains OFF')
        return None


def toggle_usb_audio_output(verbose=True):
    """Toggle both USB output digital-noise mitigations together (for an
    overall A/B comparison to rule out placebo; use
    toggle_autosuspend_guard()/toggle_rtprio_guard() to isolate a single
    cause instead).
    Does nothing if optimize_usb_audio_output() has never been called.
    Returns: the state after toggling (True=ON / False=OFF).
             None if there's no target, or the toggle failed.
    """
    global _LAST_STATE
    if _LAST_STATE.get('card_num') is None:
        if verbose:
            print('⚠️ The USB noise guard has never been applied yet (DAC may not be selected)')
        return None
    _sync_state()
    any_on = bool(_LAST_STATE.get('autosuspend_enabled') or _LAST_STATE.get('rtprio_enabled'))
    if verbose:
        print('\n' + '=' * 60)
        print(f'🔌 USB output digital-noise mitigation — {"OFF" if any_on else "ON"} (toggling both)')
        print('=' * 60)
    if any_on:
        if _LAST_STATE.get('autosuspend_enabled'):
            toggle_autosuspend_guard(verbose=verbose)
        if _LAST_STATE.get('rtprio_enabled'):
            toggle_rtprio_guard(verbose=verbose)
        still_on = bool(_LAST_STATE.get('autosuspend_enabled') or _LAST_STATE.get('rtprio_enabled'))
        result = None if still_on else False
    else:
        toggle_autosuspend_guard(verbose=verbose)
        toggle_rtprio_guard(verbose=verbose)
        now_on = bool(_LAST_STATE.get('autosuspend_enabled') or _LAST_STATE.get('rtprio_enabled'))
        result = True if now_on else None
    if verbose:
        print('=' * 60 + '\n')
    return result
