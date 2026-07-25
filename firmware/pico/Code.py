# SPDX-FileCopyrightText: Copyright (c) 2026 Tim Cocks for Adafruit Industries
# SPDX-License-Identifier: MIT

"""
AS7343 serial instrument program for Raspberry Pi Pico / CircuitPython.

Version 3 adds readable gain reporting and removes unused LED-driver output.

Original AS7343 full-test structure by Tim Cocks for Adafruit Industries.
Instrument command interface adapted for the AS7343 spectrophotometer project.

Existing commands
-----------------
d               Acquire and transmit all channels
it              Get calculated integration time in milliseconds
gg              Get gain setting
gt              Get ATIME
gs              Get ASTEP
sg <value>      Set gain
st <value>      Set ATIME
ss <value>      Set ASTEP

Additional commands
-------------------
grt             Get read timeout in milliseconds
srt <ms>        Set read timeout in milliseconds
gsm             Get SMUX channel count: 6, 12, or 18
state           Print the complete operational state on one line
help            Print the command list

Notes
-----
* SMUX remains fixed at CH18 because the Ubuntu program expects the
  18-value channel layout.
* The read timeout is never automatically reduced.
* After ATIME or ASTEP changes, the timeout is raised automatically only
  when necessary to remain safely above the estimated three-cycle
  acquisition time.
"""

import time
import board
import busio
import supervisor

from adafruit_as7343 import AS7343, Gain, SmuxMode


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_READ_TIMEOUT_MS = 3000
TIMEOUT_MARGIN_MS = 500
TIMEOUT_MULTIPLIER = 1.10

SMUX_CYCLES = {
    SmuxMode.CH6: 1,
    SmuxMode.CH12: 2,
    SmuxMode.CH18: 3,
}

SMUX_CHANNELS = {
    SmuxMode.CH6: 6,
    SmuxMode.CH12: 12,
    SmuxMode.CH18: 18,
}

SMUX_NAMES = {
    SmuxMode.CH6: "6 channels",
    SmuxMode.CH12: "12 channels (2 cycles)",
    SmuxMode.CH18: "18 channels (3 cycles)",
}

GAIN_NAMES = {
    Gain.X0_5: "0.5x",
    Gain.X1: "1x",
    Gain.X2: "2x",
    Gain.X4: "4x",
    Gain.X8: "8x",
    Gain.X16: "16x",
    Gain.X32: "32x",
    Gain.X64: "64x",
    Gain.X128: "128x",
    Gain.X256: "256x",
    Gain.X512: "512x",
    Gain.X1024: "1024x",
    Gain.X2048: "2048x",
}


# ---------------------------------------------------------------------------
# Sensor initialization
# ---------------------------------------------------------------------------

i2c = busio.I2C(board.GP1, board.GP0)  # SCL=GP1, SDA=GP0

print("AS7343 Full Test")
print("================")

try:
    sensor = AS7343(i2c)
except RuntimeError as error:
    print("Couldn't find AS7343 chip: {}".format(error))
    raise SystemExit

print("AS7343 found!")

print("\n--- Chip Information ---")
print("Part ID:     0x{:02X}".format(sensor.part_id))
print("Revision ID: 0x{:02X}".format(sensor.revision_id))
print("Aux ID:      0x{:02X}".format(sensor.aux_id))


# ---------------------------------------------------------------------------
# Instrument configuration
# ---------------------------------------------------------------------------

packet = 0

print("\n--- Spectral Configuration ---")

sensor.gain = Gain.X64
print("Gain: {}".format(GAIN_NAMES.get(sensor.gain, "Unknown")))

sensor.atime = 99
print("ATIME: {}".format(sensor.atime))

sensor.astep = 999
print("ASTEP: {}".format(sensor.astep))

sensor.read_timeout = DEFAULT_READ_TIMEOUT_MS
print("Read Timeout: {} ms".format(sensor.read_timeout))
print("Integration Time: {:.2f} ms".format(sensor.integration_time_ms))

print("\n--- SMUX Configuration ---")

# Keep CH18 fixed. The Ubuntu application expects the 18-value return layout.
sensor.smux_mode = SmuxMode.CH18
print("Mode: {}".format(SMUX_NAMES.get(sensor.smux_mode, "Unknown")))

print("\n--- Wait Time Configuration ---")

# Preserved from the original working program. Waiting remains disabled.
sensor.wtime = 100
print("Wait Time: {} (disabled by default)".format(sensor.wtime))

print("\n--- Interrupt Configuration ---")

# Preserved from the original working program. The instrument does not use
# interrupt-driven spectral acquisition.
sensor.persistence = 4
print("Persistence: {}".format(sensor.persistence))

sensor.threshold_channel = 0
print("Threshold Channel: {}".format(sensor.threshold_channel))

sensor.spectral_threshold_low = 100
print("Low Threshold: {}".format(sensor.spectral_threshold_low))

sensor.spectral_threshold_high = 60000
print("High Threshold: {}".format(sensor.spectral_threshold_high))

# The AS7343 LED driver is not used by this instrument. Keep it off,
# but do not expose or report its otherwise irrelevant current setting.
sensor.led_enabled = False

print("\n--- Spectral Readings ---")
print(
    "Channel wavelengths: F1=405nm, F2=425nm, FZ=450nm, F3=475nm, "
    "F4=515nm, F5=550nm,"
)
print(
    "FY=555nm, FXL=600nm, F6=640nm, F7=690nm, F8=745nm, "
    "NIR=855nm\n"
)

time.sleep(0.2)


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def smux_cycle_count():
    """Return the number of automatic SMUX cycles for the current mode."""
    return SMUX_CYCLES.get(sensor.smux_mode, 3)


def smux_channel_count():
    """Return the number of values produced by all_channels."""
    return SMUX_CHANNELS.get(sensor.smux_mode, 18)


def recommended_read_timeout_ms():
    """
    Estimate a safe minimum timeout for the current integration settings.

    all_channels waits for all automatic SMUX cycles. The estimate allows
    10 percent timing headroom plus 500 ms for driver and I2C overhead, with
    an absolute minimum of 3000 ms.
    """
    estimated_measurement_ms = (
        smux_cycle_count() * sensor.integration_time_ms
    )

    recommended = int(
        estimated_measurement_ms * TIMEOUT_MULTIPLIER
        + TIMEOUT_MARGIN_MS
        + 0.999
    )

    return max(DEFAULT_READ_TIMEOUT_MS, recommended)


def ensure_read_timeout():
    """
    Raise read_timeout when required, but never reduce a user-selected value.
    """
    required = recommended_read_timeout_ms()

    if sensor.read_timeout < required:
        sensor.read_timeout = required


def send_data(packet_number, readings):
    """Transmit one checksummed spectral-data packet."""
    block = ["Start", "Packet " + str(packet_number)]
    check_sum = 0

    for channel_value in readings:
        block.append(str(channel_value))

    for line in block:
        if line == "Start":
            continue

        for character in line:
            check_sum += ord(character)

    block.append(str(check_sum))
    block.append("End")

    for line in block:
        print(line)


def print_state():
    """Print the operational state as one machine-readable line."""
    gain_code = int(sensor.gain)
    gain_label = GAIN_NAMES.get(sensor.gain, "Unknown")

    print(
        "gain_code={} gain={} atime={} astep={} "
        "integration_time_ms={:.2f} read_timeout_ms={} "
        "recommended_timeout_ms={} smux_channels={} "
        "smux_cycles={}".format(
            gain_code,
            gain_label,
            sensor.atime,
            sensor.astep,
            sensor.integration_time_ms,
            sensor.read_timeout,
            recommended_read_timeout_ms(),
            smux_channel_count(),
            smux_cycle_count(),
        )
    )


def print_help():
    """Print the supported serial commands."""
    print(
        "Commands: d, it, gg, gt, gs, sg <gain>, st <atime>, "
        "ss <astep>, grt, srt <ms>, gsm, state, help"
    )


def parse_integer_argument(parts, command_name):
    """Return an integer command argument or report an input error."""
    if len(parts) != 2:
        print("ERR {} requires one integer argument".format(command_name))
        return None

    try:
        return int(parts[1])
    except ValueError:
        print("ERR {} argument must be an integer".format(command_name))
        return None


# ---------------------------------------------------------------------------
# Serial command loop
# ---------------------------------------------------------------------------

while True:
    if supervisor.runtime.serial_bytes_available:
        command_line = input().strip()
        parts = command_line.split()

        if not parts:
            continue

        cmd = parts[0].lower()

        try:
            if cmd == "d":
                readings = sensor.all_channels
                packet += 1
                send_data(packet, readings)
                continue

            if cmd == "it":
                print(sensor.integration_time_ms)
                continue

            if cmd == "gg":
                print(sensor.gain)
                continue

            if cmd == "gt":
                print(sensor.atime)
                continue

            if cmd == "gs":
                print(sensor.astep)
                continue

            if cmd == "grt":
                print(sensor.read_timeout)
                continue

            if cmd == "gsm":
                print(smux_channel_count())
                continue

            if cmd == "state":
                print_state()
                continue

            if cmd in ("help", "h", "?"):
                print_help()
                continue

            if cmd == "sg":
                target = parse_integer_argument(parts, cmd)
                if target is None:
                    continue

                sensor.gain = target
                print(sensor.gain)
                continue

            if cmd == "st":
                target = parse_integer_argument(parts, cmd)
                if target is None:
                    continue

                if target < 0 or target > 255:
                    print("ERR ATIME must be between 0 and 255")
                    continue

                sensor.atime = target
                ensure_read_timeout()
                print(sensor.atime)
                continue

            if cmd == "ss":
                target = parse_integer_argument(parts, cmd)
                if target is None:
                    continue

                if target < 0 or target > 65534:
                    print("ERR ASTEP must be between 0 and 65534")
                    continue

                sensor.astep = target
                ensure_read_timeout()
                print(sensor.astep)
                continue

            if cmd == "srt":
                target = parse_integer_argument(parts, cmd)
                if target is None:
                    continue

                minimum_timeout = recommended_read_timeout_ms()

                if target < minimum_timeout:
                    print(
                        "ERR read timeout must be at least {} ms "
                        "for the current settings".format(minimum_timeout)
                    )
                    continue

                sensor.read_timeout = target
                print(sensor.read_timeout)
                continue

            print("ERR unknown command: {}".format(cmd))

        except TimeoutError as error:
            # Preserve the command loop if a measurement exceeds the timeout.
            print("ERR spectral read timeout: {}".format(error))

        except (ValueError, RuntimeError) as error:
            # Invalid sensor property values should not terminate the program.
            print("ERR {}".format(error))

    # Preserved from the original working program.
    time.sleep(1.0)
