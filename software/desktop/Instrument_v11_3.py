"""
Instrument_v11_3.py

AS7343 / Raspberry Pi Pico serial instrument program.

Working features
----------------
- Automatically finds and opens the Pico serial port.
- Queries and sets gain, ATIME, and ASTEP.
- Queries integration time.
- Acquires a complete 18-value data block with checksum verification.
- Maps the 18 returned values to the 12 measured spectral channels.
- Removes the VIS channels by retaining channel orders 1 through 12.
- d : transient raw spectrum.
- b : acquires d-data and retains it as a blank.
- s : requires a blank, acquires d-data as a sample, and calculates
      transmittance and absorbance.
- p : adds the most recent d, b, or s measurement to a pending plot.
- sp: displays the completed overlay plot.
- ks: live absorbance kinetics at one selected wavelength.
- kd: live raw-count kinetics at one selected wavelength.
- Overlay plots use PCHIP interpolation at 1 nm intervals from
      405 through 640 nm for this white-LED setup.
- For sample data, p can select any numeric result column, including
      counts, transmittance, absorbance, or columns added later.
- Clears the stored blank when gain, ATIME, or ASTEP actually changes.
- Assigns sample IDs and retains multiple samples in memory.
- Saves the blank and all stored samples to a long-format CSV file.

Run this file with Thonny's local Python interpreter, not the
CircuitPython interpreter on the Pico.

Instrument_v11_3 adds Pico read-timeout and SMUX state support, saves the new state metadata, and adjusts the Ubuntu data-block deadline to remain longer than the Pico timeout.
"""

import glob
import signal
import time
from datetime import datetime
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.ticker import AutoMinorLocator, MultipleLocator
import numpy as np
from scipy.interpolate import PchipInterpolator
import pandas as pd
import serial
from serial.tools import list_ports


# ================================================================
# SERIAL SETTINGS
# ================================================================

# Leave as None for automatic detection.
# To choose manually, use:
# SERIAL_PORT = "/dev/ttyACM0"
SERIAL_PORT = None

BAUD_RATE = 115200

# Short timeout used by each readline().
SERIAL_POLL_TIMEOUT = 0.15

# Maximum time allowed for ordinary two-letter commands.
COMMAND_TIMEOUT = 3.0

# Minimum time allowed for a complete spectral data block. The actual
# deadline is lengthened automatically when the Pico read timeout is longer.
DATA_BLOCK_TIMEOUT = 15.0
DATA_BLOCK_TIMEOUT_MARGIN = 5.0

# Time allowed for the Pico serial connection to settle.
STARTUP_DELAY = 3.0

# Change to True to print every line received from the Pico.
DEBUG_SERIAL = False


# ================================================================
# PICO COMMAND DEFINITIONS
# ================================================================

GET_GAIN_COMMAND = "gg"
SET_GAIN_COMMAND = "sg"

GET_ATIME_COMMAND = "gt"
SET_ATIME_COMMAND = "st"

GET_ASTEP_COMMAND = "gs"
SET_ASTEP_COMMAND = "ss"

GET_INTEGRATION_TIME_COMMAND = "it"

GET_READ_TIMEOUT_COMMAND = "grt"
SET_READ_TIMEOUT_COMMAND = "srt"
GET_SMUX_MODE_COMMAND = "gsm"
GET_COMPLETE_STATE_COMMAND = "state"


# ================================================================
# VALID STATE RANGES
# ================================================================

ATIME_MIN = 0
ATIME_MAX = 255

ASTEP_MIN = 0
ASTEP_MAX = 65534


# ================================================================
# GAIN DEFINITIONS
# ================================================================

# Human-readable gains accepted by this program.
GAIN_LABELS = [
    "0.5x",
    "1x",
    "2x",
    "4x",
    "8x",
    "16x",
    "32x",
    "64x",
    "128x",
    "256x",
    "512x",
]

# Gain values corresponding to Pico gain codes 0 through 12.
# Codes 11 and 12 can be decoded if returned by the Pico, although
# this interface currently permits setting only through 512x.
VALID_GAINS = [
    0,
    1,
    2,
    4,
    8,
    16,
    32,
    64,
    128,
    256,
    512,
    1024,
    2048,
]


# ================================================================
# CHANNEL DEFINITIONS
# ================================================================

# Rows are in the exact order returned by the Pico.
# The order field places retained channels into increasing nominal
# wavelength order. make_spectrum_dataframe() retains only orders
# 1 through 12, which removes VIS and FD entries.
CHANNELS = [
    ["FZ",  450,   3,   0],
    ["FY",  555,   7,   0],
    ["FXL", 600,   8,   0],
    ["NIR", 855,  12,   0],
    ["VIS", 750,  13,   0],
    ["FD",  111, 111,   0],

    ["F2",  425,   2,   0],
    ["F3",  475,   4,   0],
    ["F4",  515,   5,   0],
    ["F6",  640,   9,   0],
    ["VIS", 999, 999,   0],
    ["FD",  111, 111,   0],

    ["F1",  405,   1,   0],
    ["F7",  690,  10,   0],
    ["F8",  745,  11,   0],
    ["F5",  550,   6,   0],
    ["VIS", 999, 999,   0],
    ["FD",  111, 111,   0],
]


# ================================================================
# PLOT CONFIGURATION
# ================================================================

# Plotting range validated for the current white-LED setup.
PLOT_RANGE_MIN_NM = 405
PLOT_RANGE_MAX_NM = 640

# Broad FY channel is excluded from plots only.
EXCLUDED_PLOT_WAVELENGTHS = {555}

# Smooth plotted curves are generated from measured points using
# shape-preserving PCHIP interpolation.
PCHIP_STEP_NM = 1


# ================================================================
# SERIAL-PORT SUPPORT
# ================================================================

def list_serial_ports():
    """Print all serial ports currently visible to Ubuntu."""

    ports = list(list_ports.comports())

    if not ports:
        print("No serial ports were found.")
        return

    print("Available serial ports:")

    for port in ports:
        description = port.description or "No description"
        print(f"  {port.device}: {description}")


def find_pico_port():
    """
    Find a likely Raspberry Pi Pico serial port.

    Returns:
        A Linux serial-device name such as /dev/ttyACM0.
    """

    candidates = []

    for port in list_ports.comports():

        description = " ".join(
            [
                port.description or "",
                port.manufacturer or "",
                port.product or "",
                port.hwid or "",
            ]
        ).lower()

        raspberry_pi_usb = port.vid == 0x2E8A
        mentions_pico = "pico" in description
        mentions_circuitpython = "circuitpython" in description

        if (
            raspberry_pi_usb
            or mentions_pico
            or mentions_circuitpython
        ):
            candidates.append(port.device)

    if not candidates:
        candidates.extend(sorted(glob.glob("/dev/ttyACM*")))

    candidates = list(dict.fromkeys(candidates))

    if not candidates:
        raise RuntimeError(
            "No likely Raspberry Pi Pico serial port was found."
        )

    if len(candidates) > 1:
        print("More than one possible Pico port was found:")

        for candidate in candidates:
            print(f"  {candidate}")

        print(f"Using {candidates[0]}")

    return candidates[0]


# ================================================================
# PICO INSTRUMENT CLASS
# ================================================================

class PicoInstrument:
    """Serial interface to the Pico AS7343 controller."""

    def __init__(self, port=None, baud_rate=BAUD_RATE):

        self.port = port if port is not None else find_pico_port()
        self.baud_rate = baud_rate
        self.serial_connection = None

        # Updated whenever grt or state is queried. This allows the
        # Ubuntu-side data-block deadline to remain longer than the
        # Pico driver's read timeout without adding a query to every
        # spectrum acquisition.
        self.read_timeout_ms_cache = None

    @property
    def connected(self):
        """Return True when the serial connection is open."""

        return (
            self.serial_connection is not None
            and self.serial_connection.is_open
        )

    def connect(self):
        """Open the serial port."""

        if self.connected:
            return

        print(f"Opening serial port {self.port}...")

        self.serial_connection = serial.Serial(
            port=self.port,
            baudrate=self.baud_rate,
            timeout=SERIAL_POLL_TIMEOUT,
            write_timeout=COMMAND_TIMEOUT,
        )

        time.sleep(STARTUP_DELAY)

        self.serial_connection.reset_input_buffer()
        self.serial_connection.reset_output_buffer()

        print("Serial port opened.")
        print()

    def close(self):
        """Close the serial port."""

        if self.serial_connection is not None:

            if self.serial_connection.is_open:
                self.serial_connection.close()

            self.serial_connection = None

        print("Serial port closed.")

    def send_command(self, command, attempts=3):
        """
        Send an ordinary command and return the first response line
        following the echoed command.
        """

        if not self.connected:
            raise RuntimeError("The Pico serial port is not open.")

        command = command.strip()

        if not command:
            raise ValueError("Cannot send an empty command.")

        for attempt in range(1, attempts + 1):

            self.serial_connection.reset_input_buffer()

            outgoing = f"{command}\r\n"

            self.serial_connection.write(
                outgoing.encode("utf-8")
            )
            self.serial_connection.flush()

            deadline = time.monotonic() + COMMAND_TIMEOUT
            echo_received = False

            while time.monotonic() < deadline:

                raw_line = self.serial_connection.readline()

                if not raw_line:
                    continue

                text = raw_line.decode(
                    "utf-8",
                    errors="ignore",
                ).strip()

                if DEBUG_SERIAL:
                    print(f"DEBUG command line: {text!r}")

                if not text:
                    continue

                if not echo_received:

                    if text.lower() == command.lower():
                        echo_received = True

                    continue

                return text

            if attempt < attempts:
                time.sleep(0.25)

        raise TimeoutError(
            f"No response received from Pico for command: {command}"
        )

    @staticmethod
    def require_success_response(response, command):
        """Raise when the Pico reports an ERR response."""

        if response.strip().upper().startswith("ERR"):
            raise ValueError(
                f"Pico rejected '{command}': {response}"
            )

        return response

    def data_block_timeout_seconds(self):
        """Return a safe Ubuntu deadline for a complete Pico block."""

        if self.read_timeout_ms_cache is None:
            return DATA_BLOCK_TIMEOUT

        pico_seconds = self.read_timeout_ms_cache / 1000.0

        return max(
            DATA_BLOCK_TIMEOUT,
            pico_seconds + DATA_BLOCK_TIMEOUT_MARGIN,
        )

    def get_data_block(self, command="d"):
        """
        Request and receive a complete spectral data block.

        Expected form:

            command echo
            Start
            packet number
            18 channel values
            checksum
            End

        Returns:
            packet_number
            expected_checksum
            calculated_checksum
            counts
        """

        if not self.connected:
            raise RuntimeError("The Pico serial port is not open.")

        command = command.strip().lower()

        if command != "d":
            raise ValueError(
                "The Pico spectral data-block command must be 'd'."
            )

        self.serial_connection.reset_input_buffer()

        outgoing = f"{command}\r\n"

        self.serial_connection.write(
            outgoing.encode("utf-8")
        )
        self.serial_connection.flush()

        data_block_timeout = self.data_block_timeout_seconds()
        deadline = time.monotonic() + data_block_timeout

        # Ignore the echo and any startup text until Start appears.
        while True:

            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Timed out waiting for Start after '{command}'."
                )

            raw_line = self.serial_connection.readline()

            if not raw_line:
                continue

            text = raw_line.decode(
                "utf-8",
                errors="ignore",
            ).strip()

            if DEBUG_SERIAL:
                print(f"DEBUG data line: {text!r}")

            if text == "Start":
                break

        received_lines = []

        while True:

            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Timed out waiting for End after '{command}'."
                )

            raw_line = self.serial_connection.readline()

            if not raw_line:
                continue

            text = raw_line.decode(
                "utf-8",
                errors="ignore",
            ).strip()

            if DEBUG_SERIAL:
                print(f"DEBUG data line: {text!r}")

            if text == "End":
                break

            if text:
                received_lines.append(text)

        # Minimum: packet number, one data value, checksum.
        if len(received_lines) < 3:
            raise ValueError(
                f"Incomplete data block received: {received_lines}"
            )

        checksum_text = received_lines[-1]
        checksum_lines = received_lines[:-1]

        try:
            expected_checksum = int(checksum_text)

        except ValueError as error:
            raise ValueError(
                f"Invalid checksum received from Pico: "
                f"{checksum_text!r}"
            ) from error

        calculated_checksum = 0

        for line in checksum_lines:
            for character in line:
                calculated_checksum += ord(character)

        packet_number = checksum_lines[0]
        count_lines = checksum_lines[1:]

        try:
            counts = [int(value) for value in count_lines]

        except ValueError as error:
            raise ValueError(
                f"Non-integer channel value in data block: "
                f"{count_lines}"
            ) from error

        return (
            packet_number,
            expected_checksum,
            calculated_checksum,
            counts,
        )

    def acquire_spectrum(self, command="d"):
        """
        Acquire one spectrum and require a valid checksum.

        Returns:
            packet_number
            counts
        """

        (
            packet_number,
            expected_checksum,
            calculated_checksum,
            counts,
        ) = self.get_data_block(command)

        if expected_checksum != calculated_checksum:
            raise ValueError(
                "Bad checksum! "
                f"Pico sent {expected_checksum}; "
                f"computer calculated {calculated_checksum}."
            )

        if len(counts) != len(CHANNELS):
            raise ValueError(
                f"Expected {len(CHANNELS)} values, "
                f"but received {len(counts)}."
            )

        return packet_number, counts

    def discard_interrupted_data_block(self):
        """
        Consume the remainder of a Pico data block after Ctrl+C.

        This restores serial synchronization before returning to the
        interactive command prompt.
        """

        if not self.connected:
            return

        deadline = time.monotonic() + DATA_BLOCK_TIMEOUT

        while time.monotonic() < deadline:

            raw_line = self.serial_connection.readline()

            if not raw_line:
                continue

            text = raw_line.decode(
                "utf-8",
                errors="ignore",
            ).strip()

            if DEBUG_SERIAL:
                print(f"DEBUG discarded line: {text!r}")

            if text == "End":
                break

        self.serial_connection.reset_input_buffer()

    # ------------------------------------------------------------
    # Gain
    # ------------------------------------------------------------

    @staticmethod
    def decode_gain_response(response):
        """Convert a Pico gain code to a readable gain label."""

        response = response.strip()

        try:
            gain_code = int(response)

        except ValueError as error:
            raise ValueError(
                f"Invalid gain response from Pico: {response!r}"
            ) from error

        if gain_code < 0 or gain_code >= len(VALID_GAINS):
            raise ValueError(
                f"Pico returned unsupported gain code: {gain_code}"
            )

        if gain_code == 0:
            return "0.5x"

        return f"{VALID_GAINS[gain_code]}x"

    def get_gain(self):
        """Return the current gain as a readable label."""

        response = self.send_command(GET_GAIN_COMMAND)
        return self.decode_gain_response(response)

    def set_gain(self, target):
        """Set gain using a readable label such as 8x."""

        target = target.strip().lower()

        if target not in GAIN_LABELS:
            valid_text = ", ".join(GAIN_LABELS)

            raise ValueError(
                f"Invalid gain setting: {target!r}. "
                f"Valid gains are: {valid_text}"
            )

        gain_code = GAIN_LABELS.index(target)

        command = f"{SET_GAIN_COMMAND} {gain_code}"
        response = self.send_command(command)
        self.require_success_response(response, command)

        return self.get_gain()

    # ------------------------------------------------------------
    # ATIME
    # ------------------------------------------------------------

    def get_atime(self):
        """Return the current ATIME value."""

        response = self.send_command(GET_ATIME_COMMAND)

        try:
            return int(response)

        except ValueError as error:
            raise ValueError(
                f"Invalid ATIME response from Pico: {response!r}"
            ) from error

    def set_atime(self, value):
        """Set ATIME and return the confirmed value."""

        try:
            value = int(value)

        except (TypeError, ValueError) as error:
            raise ValueError("ATIME must be an integer.") from error

        if value < ATIME_MIN or value > ATIME_MAX:
            raise ValueError(
                f"ATIME must be between "
                f"{ATIME_MIN} and {ATIME_MAX}."
            )

        command = f"{SET_ATIME_COMMAND} {value}"
        response = self.send_command(command)
        self.require_success_response(response, command)

        confirmed = self.get_atime()
        self.get_read_timeout()
        return confirmed

    # ------------------------------------------------------------
    # ASTEP
    # ------------------------------------------------------------

    def get_astep(self):
        """Return the current ASTEP value."""

        response = self.send_command(GET_ASTEP_COMMAND)

        try:
            return int(response)

        except ValueError as error:
            raise ValueError(
                f"Invalid ASTEP response from Pico: {response!r}"
            ) from error

    def set_astep(self, value):
        """Set ASTEP and return the confirmed value."""

        try:
            value = int(value)

        except (TypeError, ValueError) as error:
            raise ValueError("ASTEP must be an integer.") from error

        if value < ASTEP_MIN or value > ASTEP_MAX:
            raise ValueError(
                f"ASTEP must be between "
                f"{ASTEP_MIN} and {ASTEP_MAX}."
            )

        command = f"{SET_ASTEP_COMMAND} {value}"
        response = self.send_command(command)
        self.require_success_response(response, command)

        confirmed = self.get_astep()
        self.get_read_timeout()
        return confirmed

    # ------------------------------------------------------------
    # Integration time, timeout, SMUX, and complete state
    # ------------------------------------------------------------

    def get_integration_time(self):
        """Return integration time in milliseconds."""

        response = self.send_command(
            GET_INTEGRATION_TIME_COMMAND
        )

        try:
            return float(response)

        except ValueError as error:
            raise ValueError(
                "Invalid integration-time response from Pico: "
                f"{response!r}"
            ) from error

    def get_read_timeout(self):
        """Return the Pico driver read timeout in milliseconds."""

        response = self.send_command(GET_READ_TIMEOUT_COMMAND)

        try:
            value = int(response)

        except ValueError as error:
            raise ValueError(
                f"Invalid read-timeout response from Pico: {response!r}"
            ) from error

        if value <= 0:
            raise ValueError(
                f"Pico returned an invalid read timeout: {value}"
            )

        self.read_timeout_ms_cache = value
        return value

    def set_read_timeout(self, value):
        """Set the Pico driver read timeout and return confirmation."""

        try:
            value = int(value)

        except (TypeError, ValueError) as error:
            raise ValueError(
                "Read timeout must be an integer number of milliseconds."
            ) from error

        if value <= 0:
            raise ValueError(
                "Read timeout must be greater than zero."
            )

        command = f"{SET_READ_TIMEOUT_COMMAND} {value}"
        response = self.send_command(command)
        self.require_success_response(response, command)

        return self.get_read_timeout()

    def get_smux_channels(self):
        """Return the active SMUX channel count."""

        response = self.send_command(GET_SMUX_MODE_COMMAND)

        try:
            channels = int(response)

        except ValueError as error:
            raise ValueError(
                f"Invalid SMUX response from Pico: {response!r}"
            ) from error

        if channels not in (6, 12, 18):
            raise ValueError(
                f"Pico returned unsupported SMUX channel count: {channels}"
            )

        return channels

    @staticmethod
    def smux_cycles_from_channels(channels):
        """Return the automatic cycle count for a SMUX channel count."""

        return {6: 1, 12: 2, 18: 3}[channels]

    @staticmethod
    def parse_complete_state(response):
        """Parse the machine-readable response from the Pico state command."""

        values = {}

        for field in response.split():
            if "=" not in field:
                continue

            key, value = field.split("=", 1)
            values[key] = value

        required = (
            "gain_code",
            "gain",
            "atime",
            "astep",
            "integration_time_ms",
            "read_timeout_ms",
            "recommended_timeout_ms",
            "smux_channels",
            "smux_cycles",
        )

        missing = [key for key in required if key not in values]

        if missing:
            raise ValueError(
                "Incomplete Pico state response; missing: "
                + ", ".join(missing)
            )

        try:
            state = {
                "Gain": values["gain"],
                "Gain code": int(values["gain_code"]),
                "ATIME": int(values["atime"]),
                "ASTEP": int(values["astep"]),
                "Integration time": float(
                    values["integration_time_ms"]
                ),
                "Read timeout": int(values["read_timeout_ms"]),
                "Recommended timeout": int(
                    values["recommended_timeout_ms"]
                ),
                "SMUX channels": int(values["smux_channels"]),
                "SMUX cycles": int(values["smux_cycles"]),
            }

        except ValueError as error:
            raise ValueError(
                f"Invalid numeric value in Pico state response: {response!r}"
            ) from error

        return state

    def get_state(self):
        """Return the complete operational state in one Pico query."""

        response = self.send_command(GET_COMPLETE_STATE_COMMAND)
        state = self.parse_complete_state(response)
        self.read_timeout_ms_cache = state["Read timeout"]
        return state


# ================================================================
# DATAFRAME AND MEASUREMENT FUNCTIONS
# ================================================================

def make_spectrum_dataframe(counts):
    """
    Attach the 18 returned values to their channel definitions,
    retain measured channel orders 1 through 12, and sort by order.
    """

    if len(counts) != len(CHANNELS):
        raise ValueError(
            f"Expected {len(CHANNELS)} channel values, "
            f"but received {len(counts)}."
        )

    dataframe = pd.DataFrame(
        CHANNELS,
        columns=["ID", "wavelength", "order", "counts"],
    )

    dataframe["counts"] = counts

    # Retain only the 12 measured spectral channels. This removes
    # the VIS and FD rows from the working spectrum.
    dataframe = dataframe[
        dataframe["order"].between(1, 12)
    ]

    dataframe = dataframe.sort_values(by="order")
    dataframe = dataframe.reset_index(drop=True)

    return dataframe


class MeasurementSession:
    """
    Holds transient data, the retained blank, multiple samples,
    and the pending overlay plot.
    """

    def __init__(self):

        self.last_data = None
        self.last_packet = None

        self.blank_data = None
        self.blank_packet = None
        self.blank_state = None
        self.blank_timestamp = None

        # Dictionary insertion order preserves acquisition order.
        self.samples = {}

        # The next sample receives sample_001, sample_002, and so on
        # automatically.  pending_sample_id is an optional one-sample
        # override set by the id command.
        self.next_sample_number = 1
        self.pending_sample_id = None

        # Most recent Pico measurement available to the p command.
        self.latest_measurement_type = None
        self.latest_measurement_label = None
        self.latest_packet = None
        self.latest_plot_data = None

        # Curves waiting for the sp command.
        self.pending_plot_series = []
        self.pending_plot_kind = None

    def record_latest_measurement(
        self,
        measurement_type,
        packet_number,
        dataframe,
        label,
    ):
        """Record the most recent d, b, or s result for plotting."""

        self.latest_measurement_type = measurement_type
        self.latest_measurement_label = label
        self.latest_packet = packet_number
        self.latest_plot_data = dataframe.copy()

    def store_transient(self, packet_number, dataframe):
        """Store the most recently acquired transient spectrum."""

        self.last_packet = packet_number
        self.last_data = dataframe.copy()

        self.record_latest_measurement(
            measurement_type="d",
            packet_number=packet_number,
            dataframe=dataframe,
            label=f"d {packet_number}",
        )

    def store_blank(
        self,
        packet_number,
        dataframe,
        instrument_state,
    ):
        """
        Store a new blank.

        A new blank starts a new measurement set, so all samples
        calculated from the previous blank are cleared.
        """

        self.last_packet = packet_number
        self.last_data = dataframe.copy()

        self.blank_packet = packet_number
        self.blank_data = dataframe.copy()
        self.blank_state = instrument_state.copy()
        self.blank_timestamp = current_timestamp()

        self.samples.clear()
        self.next_sample_number = 1
        self.pending_sample_id = None

        self.record_latest_measurement(
            measurement_type="b",
            packet_number=packet_number,
            dataframe=dataframe,
            label=f"blank {packet_number}",
        )

    def store_sample(
        self,
        sample_id,
        packet_number,
        dataframe,
        results,
        instrument_state,
    ):
        """Store one identified sample and its calculated results."""

        if sample_id in self.samples:
            raise ValueError(
                f"Sample ID {sample_id!r} already exists."
            )

        self.last_packet = packet_number
        self.last_data = dataframe.copy()

        self.samples[sample_id] = {
            "sample_id": sample_id,
            "packet": packet_number,
            "timestamp": current_timestamp(),
            "state": instrument_state.copy(),
            "data": dataframe.copy(),
            "results": results.copy(),
        }

        self.record_latest_measurement(
            measurement_type="s",
            packet_number=packet_number,
            dataframe=results,
            label=sample_id,
        )

        # An explicit id command applies to one sample only.
        self.pending_sample_id = None

        # Advance the automatic sequence after every successful sample.
        self.next_sample_number += 1

    def get_next_sample_id(self):
        """Return the effective ID for the next sample."""

        if self.pending_sample_id is not None:
            return self.pending_sample_id

        sample_number = self.next_sample_number

        while True:
            sample_id = f"sample_{sample_number:03d}"

            if sample_id not in self.samples:
                return sample_id

            sample_number += 1

    def set_pending_sample_id(self, sample_id):
        """Set the ID to be used by the next sample acquisition."""

        sample_id = sample_id.strip()

        if not sample_id:
            raise ValueError("Sample ID cannot be empty.")

        if sample_id in self.samples:
            raise ValueError(
                f"Sample ID {sample_id!r} already exists."
            )

        self.pending_sample_id = sample_id

    def invalidate_blank(self):
        """
        Clear the blank and every sample calculated from it.

        Returns:
            True if a blank existed; otherwise False.
        """

        blank_existed = self.blank_data is not None

        self.blank_data = None
        self.blank_packet = None
        self.blank_state = None
        self.blank_timestamp = None

        self.samples.clear()
        self.next_sample_number = 1
        self.pending_sample_id = None

        return blank_existed


def capture_measurement_state(instrument):
    """Capture complete instrument metadata in one Pico query."""

    return instrument.get_state()


def measurement_states_match(blank_state, current_state):
    """Return True when the important blank/sample settings match."""

    for key in ("Gain", "ATIME", "ASTEP"):

        if blank_state.get(key) != current_state.get(key):
            return False

    return True


def calculate_sample_results(blank_df, sample_df):
    """
    Calculate channel-by-channel transmittance and absorbance.

        T = sample counts / blank counts
        A = -log10(T)
    """

    blank_orders = blank_df["order"].tolist()
    sample_orders = sample_df["order"].tolist()

    if blank_orders != sample_orders:
        raise ValueError(
            "Blank and sample channel orders do not match."
        )

    results = sample_df[
        ["ID", "wavelength", "order"]
    ].copy()

    results["blank_counts"] = (
        blank_df["counts"].astype(float).to_numpy()
    )

    results["sample_counts"] = (
        sample_df["counts"].astype(float).to_numpy()
    )

    results["transmittance"] = np.nan
    results["absorbance"] = np.nan

    valid_blank = results["blank_counts"] > 0

    results.loc[
        valid_blank,
        "transmittance",
    ] = (
        results.loc[valid_blank, "sample_counts"]
        / results.loc[valid_blank, "blank_counts"]
    )

    valid_transmittance = results["transmittance"] > 0

    results.loc[
        valid_transmittance,
        "absorbance",
    ] = -np.log10(
        results.loc[
            valid_transmittance,
            "transmittance",
        ]
    )

    return results


def current_timestamp():
    """Return the current local timestamp in ISO format."""

    return datetime.now().astimezone().isoformat(
        timespec="seconds"
    )


def normalize_csv_path(filename):
    """
    Convert user-supplied text to a CSV Path.

    A .csv extension is added when no extension is supplied.
    """

    filename = filename.strip()

    if not filename:
        raise ValueError("CSV filename cannot be empty.")

    path = Path(filename).expanduser()

    if path.suffix == "":
        path = path.with_suffix(".csv")

    if path.suffix.lower() != ".csv":
        raise ValueError(
            "Experiment files must use the .csv extension."
        )

    return path


def build_experiment_dataframe(session):
    """
    Build one long-format table containing the blank and all samples.

    One row represents one retained spectral channel.
    """

    if session.blank_data is None:
        raise ValueError(
            "There is no stored blank to save."
        )

    rows = []

    blank_state = session.blank_state

    for _, row in session.blank_data.iterrows():

        rows.append(
            {
                "record_type": "blank",
                "sample_id": "blank",
                "timestamp": session.blank_timestamp,
                "packet": session.blank_packet,
                "gain": blank_state["Gain"],
                "atime": blank_state["ATIME"],
                "astep": blank_state["ASTEP"],
                "integration_time_ms": (
                    blank_state["Integration time"]
                ),
                "read_timeout_ms": blank_state["Read timeout"],
                "recommended_timeout_ms": (
                    blank_state["Recommended timeout"]
                ),
                "smux_channels": blank_state["SMUX channels"],
                "smux_cycles": blank_state["SMUX cycles"],
                "channel_id": row["ID"],
                "wavelength_nm": int(row["wavelength"]),
                "order": int(row["order"]),
                "counts": int(row["counts"]),
                "blank_counts": int(row["counts"]),
                "transmittance": 1.0,
                "absorbance": 0.0,
            }
        )

    for sample_id, sample in session.samples.items():

        state = sample["state"]
        results = sample["results"]

        for _, row in results.iterrows():

            rows.append(
                {
                    "record_type": "sample",
                    "sample_id": sample_id,
                    "timestamp": sample["timestamp"],
                    "packet": sample["packet"],
                    "gain": state["Gain"],
                    "atime": state["ATIME"],
                    "astep": state["ASTEP"],
                    "integration_time_ms": (
                        state["Integration time"]
                    ),
                    "read_timeout_ms": state["Read timeout"],
                    "recommended_timeout_ms": (
                        state["Recommended timeout"]
                    ),
                    "smux_channels": state["SMUX channels"],
                    "smux_cycles": state["SMUX cycles"],
                    "channel_id": row["ID"],
                    "wavelength_nm": int(row["wavelength"]),
                    "order": int(row["order"]),
                    "counts": int(row["sample_counts"]),
                    "blank_counts": int(row["blank_counts"]),
                    "transmittance": float(
                        row["transmittance"]
                    ),
                    "absorbance": float(row["absorbance"]),
                }
            )

    columns = [
        "record_type",
        "sample_id",
        "timestamp",
        "packet",
        "gain",
        "atime",
        "astep",
        "integration_time_ms",
        "read_timeout_ms",
        "recommended_timeout_ms",
        "smux_channels",
        "smux_cycles",
        "channel_id",
        "wavelength_nm",
        "order",
        "counts",
        "blank_counts",
        "transmittance",
        "absorbance",
    ]

    return pd.DataFrame(rows, columns=columns)


def save_experiment_csv(
    session,
    filename,
    overwrite=False,
):
    """
    Save the blank and all retained samples to a CSV file.

    The file is written atomically through a temporary file.
    """

    path = normalize_csv_path(filename)

    if path.exists() and not overwrite:
        raise FileExistsError(
            f"{path} already exists. "
            f"Use 'save! {path}' to replace it."
        )

    path.parent.mkdir(parents=True, exist_ok=True)

    dataframe = build_experiment_dataframe(session)

    temporary_path = path.with_name(
        path.name + ".temporary"
    )

    dataframe.to_csv(
        temporary_path,
        index=False,
        float_format="%.8f",
    )

    temporary_path.replace(path)

    return path, len(dataframe)


def print_tab_delimited(dataframe):
    """
    Print a DataFrame as tab-delimited text.

    Floating-point values are printed with six decimal places.
    The output can be copied directly from Thonny and pasted into
    Excel, LibreOffice Calc, or another spreadsheet.
    """

    print(
        dataframe.to_csv(
            sep="\t",
            index=False,
            lineterminator="\n",
            float_format="%.6f",
        ),
        end="",
    )


# ================================================================
# DISPLAY FUNCTIONS
# ================================================================

def print_state(state):
    """Print the complete instrument state."""

    print("AS7343 Instrument State")
    print("=" * 40)

    for label, value in state.items():

        if (
            label in (
                "Integration time",
                "Read timeout",
                "Recommended timeout",
            )
            and not isinstance(value, str)
        ):
            print(f"{label:20s}: {value} ms")
        else:
            print(f"{label:20s}: {value}")

    print()


def print_timing_state(instrument):
    """Print timing settings, including timeout protection."""

    state = instrument.get_state()

    print(f"ATIME:              {state['ATIME']}")
    print(f"ASTEP:              {state['ASTEP']}")
    print(
        f"Integration time:   "
        f"{state['Integration time']} ms"
    )
    print(f"Read timeout:       {state['Read timeout']} ms")
    print(
        f"Recommended timeout: "
        f"{state['Recommended timeout']} ms"
    )


def print_raw_spectrum(dataframe):
    """
    Print raw spectral data with compact spreadsheet-friendly headers.
    """

    display_df = dataframe[
        ["ID", "wavelength", "order", "counts"]
    ].copy()

    display_df = display_df.rename(
        columns={
            "wavelength": "nm",
        }
    )

    print_tab_delimited(display_df)


def print_sample_results(results):
    """
    Print sample results using compact tab-delimited headers.

    Display headers:
        ID, nm, blank, sample, T, A
    """

    display_df = results[
        [
            "ID",
            "wavelength",
            "blank_counts",
            "sample_counts",
            "transmittance",
            "absorbance",
        ]
    ].copy()

    display_df = display_df.rename(
        columns={
            "wavelength": "nm",
            "blank_counts": "blank",
            "sample_counts": "sample",
            "transmittance": "T",
            "absorbance": "A",
        }
    )

    display_df["blank"] = display_df["blank"].astype(int)
    display_df["sample"] = display_df["sample"].astype(int)

    print_tab_delimited(display_df)


# ================================================================
# PLOTTING SUPPORT
# ================================================================

PLOT_COLUMN_ALIASES = {
    "a": "absorbance",
    "abs": "absorbance",
    "absorption": "absorbance",
    "absorbtion": "absorbance",
    "t": "transmittance",
    "transmission": "transmittance",
    "sample": "sample_counts",
    "blank": "blank_counts",
}


def normalize_plot_column_name(name):
    """Normalize a user-entered or DataFrame plot-column name."""

    return (
        str(name).strip()
        .lower()
        .replace("%", "percent")
        .replace("-", "_")
        .replace(" ", "_")
    )


def available_numeric_plot_columns(dataframe):
    """Return numeric columns that can sensibly be plotted."""

    excluded = {"wavelength", "order"}

    return [
        column
        for column in dataframe.columns
        if (
            column not in excluded
            and pd.api.types.is_numeric_dtype(dataframe[column])
        )
    ]


def resolve_plot_column(session, requested_column=None):
    """
    Determine which column of the latest measurement will be plotted.

    Defaults:
        d or b -> counts
        s      -> absorbance

    For an s result, user-facing 'counts' means sample_counts.
    """

    dataframe = session.latest_plot_data
    measurement_type = session.latest_measurement_type

    if dataframe is None or measurement_type is None:
        raise ValueError(
            "No Pico measurement is currently available to plot."
        )

    if requested_column is None:
        requested_column = (
            "absorbance"
            if measurement_type == "s"
            else "counts"
        )

    normalized_request = normalize_plot_column_name(
        requested_column
    )

    normalized_request = PLOT_COLUMN_ALIASES.get(
        normalized_request,
        normalized_request,
    )

    if measurement_type in ("d", "b"):

        if normalized_request != "counts":
            raise ValueError(
                f"The most recent measurement was '{measurement_type}'. "
                "Only counts can be plotted from d or b data."
            )

        source_column = "counts"

    else:

        # For sample results, plain counts means the sample counts.
        if normalized_request == "counts":
            source_column = "sample_counts"

        else:
            normalized_columns = {
                normalize_plot_column_name(column): column
                for column in dataframe.columns
            }

            source_column = normalized_columns.get(
                normalized_request
            )

            if source_column is None:
                available_text = ", ".join(
                    available_numeric_plot_columns(dataframe)
                )

                raise ValueError(
                    f"Plot column {requested_column!r} is not "
                    f"available. Available numeric columns: "
                    f"{available_text}"
                )

    if source_column not in dataframe.columns:
        raise ValueError(
            f"Column {source_column!r} is not present in the "
            "latest measurement."
        )

    if not pd.api.types.is_numeric_dtype(dataframe[source_column]):
        raise ValueError(
            f"Column {source_column!r} is not numeric."
        )

    # All count columns can be combined on a counts overlay.
    if (
        source_column == "counts"
        or source_column.endswith("_counts")
    ):
        plot_kind = "counts"
    else:
        plot_kind = normalize_plot_column_name(source_column)

    return source_column, plot_kind


def add_latest_measurement_to_plot(
    session,
    requested_column=None,
):
    """
    Add the selected latest-measurement column to the pending overlay.

    Nothing is displayed until the sp command is entered.
    """

    source_column, plot_kind = resolve_plot_column(
        session,
        requested_column,
    )

    if (
        session.pending_plot_kind is not None
        and session.pending_plot_kind != plot_kind
    ):
        raise ValueError(
            f"The pending overlay contains "
            f"{session.pending_plot_kind} data. Cannot add "
            f"{plot_kind} data to the same plot. Use sp to "
            "show the current overlay first."
        )

    dataframe = session.latest_plot_data

    # Plot only the empirically supported white-LED range, and omit
    # the broad 555 nm FY channel from all plots. These channels
    # remain available in stored measurement data and CSV output.
    plot_mask = (
        dataframe["wavelength"].between(
            PLOT_RANGE_MIN_NM,
            PLOT_RANGE_MAX_NM,
        )
        & ~dataframe["wavelength"].isin(EXCLUDED_PLOT_WAVELENGTHS)
    )

    plot_df = dataframe.loc[
        plot_mask,
        ["wavelength", source_column],
    ].copy()

    plot_df[source_column] = pd.to_numeric(
        plot_df[source_column],
        errors="coerce",
    )

    plot_df = plot_df.replace([np.inf, -np.inf], np.nan)

    plot_df = plot_df.dropna(
        subset=["wavelength", source_column]
    )

    plot_df = plot_df.sort_values(
        by="wavelength"
    ).reset_index(drop=True)

    if plot_df.empty:
        raise ValueError(
            f"Column {source_column!r} contains no finite values "
            f"between {PLOT_RANGE_MIN_NM} and {PLOT_RANGE_MAX_NM} nm."
        )

    session.pending_plot_series.append(
        {
            "wavelength": plot_df[
                "wavelength"
            ].to_numpy(copy=True),
            "values": plot_df[
                source_column
            ].to_numpy(copy=True),
            "label": session.latest_measurement_label,
            "packet": session.latest_packet,
            "source_column": source_column,
        }
    )

    session.pending_plot_kind = plot_kind

    return {
        "label": session.latest_measurement_label,
        "column": source_column,
        "points": len(plot_df),
        "pending": len(session.pending_plot_series),
    }


def format_plot_axis_label(plot_kind):
    """Convert an internal plot kind to a readable y-axis label."""

    labels = {
        "counts": "Counts",
        "transmittance": "Transmittance",
        "absorbance": "Absorbance",
    }

    if plot_kind in labels:
        return labels[plot_kind]

    return plot_kind.replace("_", " ").title()


def build_pchip_curve(wavelengths, values):
    """Return 1 nm PCHIP interpolation over the validated plot range."""

    x_measured = np.asarray(wavelengths, dtype=float)
    y_measured = np.asarray(values, dtype=float)

    if x_measured.size < 2:
        raise ValueError(
            "At least two measured points are required for PCHIP plotting."
        )

    interpolator = PchipInterpolator(
        x_measured,
        y_measured,
        extrapolate=False,
    )

    x_smooth = np.arange(
        PLOT_RANGE_MIN_NM,
        PLOT_RANGE_MAX_NM + PCHIP_STEP_NM,
        PCHIP_STEP_NM,
        dtype=float,
    )

    y_smooth = interpolator(x_smooth)

    valid = np.isfinite(y_smooth)

    return x_smooth[valid], y_smooth[valid]


def show_pending_plot(session):
    """
    Display the completed overlay and clear the pending plot queue.

    The displayed figure remains open. The next p command starts a
    new overlay queue.
    """

    if not session.pending_plot_series:
        raise ValueError(
            "There are no pending curves to show. Use p after a "
            "d, b, or s measurement first."
        )

    plot_kind = session.pending_plot_kind
    y_axis_label = format_plot_axis_label(plot_kind)

    figure, axis = plt.subplots(figsize=(9, 5.5))

    for series in session.pending_plot_series:
        x_smooth, y_smooth = build_pchip_curve(
            series["wavelength"],
            series["values"],
        )

        smooth_line, = axis.plot(
            x_smooth,
            y_smooth,
            linestyle="-",
            linewidth=1.5,
            zorder=3,
            label=series["label"],
        )

        axis.plot(
            series["wavelength"],
            series["values"],
            linestyle="none",
            marker="o",
            color=smooth_line.get_color(),
            zorder=4,
            label="_nolegend_",
        )

    # The axis is focused on the empirically supported white-LED
    # range. Smooth curves are PCHIP interpolations at 1 nm intervals
    # from 405 through 640 nm, with 555 nm omitted from the measured
    # input points.
    axis.set_xlim(400, 650)

    # Explicitly activate minor ticks before assigning their locators.
    axis.minorticks_on()

    axis.xaxis.set_major_locator(MultipleLocator(50))
    axis.xaxis.set_minor_locator(MultipleLocator(10))

    # Place one horizontal minor grid line halfway between adjacent
    # major y-axis ticks.
    axis.yaxis.set_minor_locator(AutoMinorLocator(2))

    axis.set_xlabel("Wavelength (nm)")
    axis.set_ylabel(y_axis_label)
    axis.set_title(
        f"AS7343 {y_axis_label} Overlay "
        f"(PCHIP, 1 nm, 405–640 nm)"
    )

    # Draw grids beneath curves and markers.
    axis.set_axisbelow(True)

    # Activate major grids on each axis explicitly.
    axis.xaxis.grid(
        visible=True,
        which="major",
        color="0.62",
        linewidth=0.8,
        zorder=1,
    )

    axis.yaxis.grid(
        visible=True,
        which="major",
        color="0.62",
        linewidth=0.8,
        zorder=1,
    )

    # Draw minor grid lines explicitly. They must have a zorder above
    # the axes background patch but below the data curves and markers.
    x_min, x_max = axis.get_xlim()

    for wavelength in np.arange(
        np.ceil(x_min / 10.0) * 10.0,
        x_max + 0.1,
        10.0,
    ):
        # Major vertical grids already occur every 50 nm.
        if not np.isclose(wavelength % 50.0, 0.0):
            axis.axvline(
                wavelength,
                color="0.78",
                linewidth=0.60,
                zorder=1.5,
            )

    # Ask Matplotlib for the current major y ticks and place one minor
    # horizontal guide halfway between each adjacent pair.
    figure.canvas.draw()
    y_min, y_max = axis.get_ylim()
    major_y_ticks = axis.get_yticks()

    for lower_tick, upper_tick in zip(
        major_y_ticks[:-1],
        major_y_ticks[1:],
    ):
        minor_y = (lower_tick + upper_tick) / 2.0

        if y_min < minor_y < y_max:
            axis.axhline(
                minor_y,
                color="0.78",
                linewidth=0.60,
                zorder=1.5,
            )

    # Preserve the original automatic y limits after adding guides.
    axis.set_ylim(y_min, y_max)

    axis.tick_params(
        axis="x",
        which="minor",
        length=4,
    )

    axis.tick_params(
        axis="y",
        which="minor",
        length=3,
    )

    axis.legend()
    figure.tight_layout()

    number_shown = len(session.pending_plot_series)

    # Use a blocking display so Matplotlib's GUI event loop remains
    # active while the plot window is open. Close the plot window to
    # return to the Pico command prompt. This is reliable on Ubuntu
    # terminals, where input() otherwise starves the GUI event loop.
    figure.canvas.draw()
    plt.show(block=True)

    # After the displayed plot is closed, start a new overlay queue.
    session.pending_plot_series.clear()
    session.pending_plot_kind = None

    return number_shown


# ================================================================
# KINETICS SUPPORT
# ================================================================

# Wavelengths supported by the retained AS7343 spectral channels.
KINETICS_WAVELENGTHS = {
    405,
    425,
    450,
    475,
    515,
    550,
    600,
    640,
    690,
    745,
    855,
}

# Empirically supported range for the standard white-LED setup.
VALIDATED_WHITE_LED_WAVELENGTHS = {
    405,
    425,
    450,
    475,
    515,
    550,
    600,
    640,
}


def parse_kinetics_command(command):
    """
    Parse a ks or kd command.

    Accepted forms:
        ks wavelength interval_seconds
        ks wavelength interval_seconds maximum_points
        kd wavelength interval_seconds
        kd wavelength interval_seconds maximum_points
    """

    parts = command.split()

    if len(parts) not in (3, 4):
        raise ValueError(
            "Use: ks wavelength interval_seconds [maximum_points] "
            "or kd wavelength interval_seconds [maximum_points]"
        )

    mode_command = parts[0].lower()

    if mode_command not in ("ks", "kd"):
        raise ValueError("Kinetics mode must be ks or kd.")

    try:
        wavelength = int(parts[1])

    except ValueError as error:
        raise ValueError(
            "Kinetics wavelength must be an integer AS7343 "
            "wavelength."
        ) from error

    if wavelength not in KINETICS_WAVELENGTHS:
        allowed = ", ".join(
            str(value)
            for value in sorted(KINETICS_WAVELENGTHS)
        )

        raise ValueError(
            f"Unsupported kinetics wavelength: {wavelength} nm. "
            f"Allowed wavelengths: {allowed}"
        )

    try:
        interval_seconds = float(parts[2])

    except ValueError as error:
        raise ValueError(
            "Kinetics interval must be a number of seconds."
        ) from error

    if interval_seconds <= 0:
        raise ValueError(
            "Kinetics interval must be greater than zero."
        )

    maximum_points = None

    if len(parts) == 4:

        try:
            maximum_points = int(parts[3])

        except ValueError as error:
            raise ValueError(
                "Maximum points must be a positive integer."
            ) from error

        if maximum_points <= 0:
            raise ValueError(
                "Maximum points must be a positive integer."
            )

    return {
        "mode": "absorbance" if mode_command == "ks" else "counts",
        "command": mode_command,
        "wavelength": wavelength,
        "interval_seconds": interval_seconds,
        "maximum_points": maximum_points,
    }


def unique_kinetics_paths(mode, wavelength):
    """Return unused CSV and PNG paths for one kinetic run."""

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = f"kinetics_{timestamp}_{mode}_{wavelength}nm"
    directory = Path.cwd()
    csv_path = directory / f"{stem}.csv"
    png_path = directory / f"{stem}.png"
    suffix = 2

    while csv_path.exists() or png_path.exists():
        csv_path = directory / f"{stem}_{suffix}.csv"
        png_path = directory / f"{stem}_{suffix}.png"
        suffix += 1

    return csv_path, png_path


def get_exact_wavelength_row(dataframe, wavelength):
    """Return the row corresponding to one exact AS7343 wavelength."""

    matching = dataframe.loc[
        dataframe["wavelength"] == wavelength
    ]

    if matching.empty:
        raise ValueError(
            f"No {wavelength} nm channel was found in the spectrum."
        )

    return matching.iloc[0]


def build_kinetics_rows(
    run_id,
    mode,
    point_number,
    selected_wavelength,
    packet_number,
    timestamp,
    scheduled_seconds,
    acquisition_start_seconds,
    acquisition_end_seconds,
    elapsed_seconds,
    acquisition_seconds,
    instrument_state,
    spectrum_df,
    results_df=None,
):
    """Build long-format rows for one completed kinetic point."""

    rows = []

    if results_df is not None:
        results_by_order = results_df.set_index("order")
    else:
        results_by_order = None

    for _, spectrum_row in spectrum_df.iterrows():

        order = int(spectrum_row["order"])
        wavelength = int(spectrum_row["wavelength"])

        row = {
            "run_id": run_id,
            "mode": mode,
            "point": point_number,
            "timestamp": timestamp,
            "scheduled_seconds": scheduled_seconds,
            "acquisition_start_seconds": (
                acquisition_start_seconds
            ),
            "acquisition_end_seconds": acquisition_end_seconds,
            "elapsed_seconds": elapsed_seconds,
            "acquisition_seconds": acquisition_seconds,
            "packet": packet_number,
            "selected_wavelength_nm": selected_wavelength,
            "channel_id": spectrum_row["ID"],
            "wavelength_nm": wavelength,
            "order": order,
            "is_monitored": wavelength == selected_wavelength,
            "counts": int(spectrum_row["counts"]),
            "blank_counts": np.nan,
            "transmittance": np.nan,
            "absorbance": np.nan,
            "gain": instrument_state["Gain"],
            "atime": instrument_state["ATIME"],
            "astep": instrument_state["ASTEP"],
            "integration_time_ms": (
                instrument_state["Integration time"]
            ),
            "read_timeout_ms": instrument_state["Read timeout"],
            "recommended_timeout_ms": (
                instrument_state["Recommended timeout"]
            ),
            "smux_channels": instrument_state["SMUX channels"],
            "smux_cycles": instrument_state["SMUX cycles"],
        }

        if results_by_order is not None:
            result_row = results_by_order.loc[order]
            row["blank_counts"] = int(
                result_row["blank_counts"]
            )
            row["transmittance"] = float(
                result_row["transmittance"]
            )
            row["absorbance"] = float(
                result_row["absorbance"]
            )

        rows.append(row)

    return rows


def append_kinetics_rows(csv_path, rows):
    """Append one completed kinetic point to its CSV file."""

    dataframe = pd.DataFrame(rows)
    write_header = not csv_path.exists()

    dataframe.to_csv(
        csv_path,
        mode="a",
        header=write_header,
        index=False,
    )


def refresh_kinetics_minor_guides(
    axis,
    figure,
    existing_artists,
):
    """Redraw explicit minor grid lines for the live kinetic plot."""

    for artist in existing_artists:
        artist.remove()

    existing_artists.clear()

    figure.canvas.draw()
    x_min, x_max = axis.get_xlim()
    y_min, y_max = axis.get_ylim()

    major_x_ticks = axis.get_xticks()
    major_y_ticks = axis.get_yticks()

    for lower_tick, upper_tick in zip(
        major_x_ticks[:-1],
        major_x_ticks[1:],
    ):
        minor_x = (lower_tick + upper_tick) / 2.0

        if x_min < minor_x < x_max:
            existing_artists.append(
                axis.axvline(
                    minor_x,
                    color="0.78",
                    linewidth=0.60,
                    zorder=1.5,
                )
            )

    for lower_tick, upper_tick in zip(
        major_y_ticks[:-1],
        major_y_ticks[1:],
    ):
        minor_y = (lower_tick + upper_tick) / 2.0

        if y_min < minor_y < y_max:
            existing_artists.append(
                axis.axhline(
                    minor_y,
                    color="0.78",
                    linewidth=0.60,
                    zorder=1.5,
                )
            )

    axis.set_xlim(x_min, x_max)
    axis.set_ylim(y_min, y_max)


def responsive_wait_until(target_time, figure, stop_state=None):
    """
    Wait until a target monotonic time while servicing plot events.

    Returns False if the plot window was closed or a stop was
    requested with Ctrl+C.
    """

    while True:

        if stop_state is not None and stop_state.get("requested"):
            return False

        if not plt.fignum_exists(figure.number):
            return False

        remaining = target_time - time.monotonic()

        if remaining <= 0:
            return True

        plt.pause(min(0.05, remaining))


def update_kinetics_plot(
    figure,
    axis,
    line,
    status_text,
    elapsed_values,
    measured_values,
    mode,
    wavelength,
    point_number,
    interval_seconds,
    minor_grid_artists,
):
    """Update the live kinetic plot after one completed point."""

    line.set_data(elapsed_values, measured_values)

    # Recalculate both axes explicitly after every point. The first
    # kinetics version set a manual y range after point 1; that turned
    # off Matplotlib's later y autoscaling and allowed a fast reaction
    # to disappear below the visible plot.
    axis.set_xlim(
        left=0,
        right=max(elapsed_values[-1] * 1.05, interval_seconds, 1.0),
    )

    value_min = float(np.min(measured_values))
    value_max = float(np.max(measured_values))
    value_span = value_max - value_min
    value_center = (value_min + value_max) / 2.0

    if mode == "absorbance":
        minimum_padding = 0.01
    else:
        minimum_padding = max(abs(value_center) * 0.02, 1.0)

    if value_span > 0:
        y_padding = max(value_span * 0.10, minimum_padding)
    else:
        y_padding = max(abs(value_center) * 0.08, minimum_padding)

    axis.set_ylim(
        value_min - y_padding,
        value_max + y_padding,
    )

    status_text.set_text(
        f"Point {point_number}   "
        f"t = {elapsed_values[-1]:.2f} s   "
        f"{mode.title()} = {measured_values[-1]:.6g}"
    )

    refresh_kinetics_minor_guides(
        axis,
        figure,
        minor_grid_artists,
    )

    figure.canvas.draw_idle()
    figure.canvas.flush_events()
    plt.pause(0.01)


def run_kinetics(instrument, session, settings):
    """Run one armed, live, incrementally saved kinetic experiment."""

    mode = settings["mode"]
    wavelength = settings["wavelength"]
    interval_seconds = settings["interval_seconds"]
    maximum_points = settings["maximum_points"]

    if mode == "absorbance":

        if session.blank_data is None:
            raise ValueError(
                "Absorbance kinetics requires a stored blank."
            )

        current_state = capture_measurement_state(instrument)

        if not measurement_states_match(
            session.blank_state,
            current_state,
        ):
            session.invalidate_blank()
            raise ValueError(
                "Instrument settings changed after the blank. "
                "The old blank was cleared; acquire a new blank."
            )
    else:
        current_state = capture_measurement_state(instrument)

    # Create and paint the live plot before arming. The timer does not
    # start until ENTER is pressed, so the first acquisition can begin
    # immediately at time zero.
    plt.ion()
    figure, axis = plt.subplots(figsize=(9, 5.5))

    line, = axis.plot(
        [],
        [],
        marker="o",
        linestyle="-",
        linewidth=1.5,
        zorder=3,
    )

    axis.set_xlabel("Elapsed time (s)")
    axis.set_ylabel("Absorbance" if mode == "absorbance" else "Counts")
    axis.set_title(
        f"AS7343 {mode.title()} Kinetics at {wavelength} nm"
    )
    axis.set_axisbelow(True)

    axis.xaxis.grid(
        visible=True,
        which="major",
        color="0.62",
        linewidth=0.8,
        zorder=1,
    )
    axis.yaxis.grid(
        visible=True,
        which="major",
        color="0.62",
        linewidth=0.8,
        zorder=1,
    )

    status_text = axis.text(
        0.02,
        0.97,
        "KINETICS ARMED — press ENTER in the terminal",
        transform=axis.transAxes,
        verticalalignment="top",
    )

    minor_grid_artists = []
    figure.tight_layout()
    figure.canvas.draw()
    plt.show(block=False)
    plt.pause(0.05)

    print()
    print("KINETICS ARMED")
    print(f"Mode:             {mode}")
    print(f"Wavelength:       {wavelength} nm")
    print(f"Interval:         {interval_seconds:g} seconds")

    if maximum_points is None:
        print("Maximum points:   Until Ctrl+C")
    else:
        print(f"Maximum points:   {maximum_points}")

    if mode == "absorbance":
        print(f"Blank packet:     {session.blank_packet}")

    if wavelength not in VALIDATED_WHITE_LED_WAVELENGTHS:
        print(
            "WARNING: This wavelength is outside the validated "
            "405-640 nm white-LED range."
        )

    print()
    print("Press ENTER to start.")
    print("Press Ctrl+C to cancel before starting.")

    try:
        input()

    except KeyboardInterrupt:
        print()
        print("Kinetic run cancelled before start.")
        plt.close(figure)
        plt.ioff()
        return

    run_id = datetime.now().strftime("run_%Y%m%d_%H%M%S")

    csv_path, png_path = unique_kinetics_paths(
        mode,
        wavelength,
    )

    run_zero = time.monotonic()

    elapsed_values = []
    measured_values = []
    completed_points = 0
    acquisition_in_progress = False
    stopped_reason = "completed"
    warned_about_interval = False

    # During a live run, use a signal flag rather than allowing
    # SIGINT to raise KeyboardInterrupt inside Tkinter callbacks.
    # The current acquisition is allowed to finish; the run then
    # stops cleanly and saves every completed point.
    stop_state = {"requested": False}
    previous_sigint_handler = signal.getsignal(signal.SIGINT)

    def request_kinetics_stop(signum, frame):
        stop_state["requested"] = True

    signal.signal(signal.SIGINT, request_kinetics_stop)

    status_text.set_text("Waiting for first point...")
    figure.canvas.draw_idle()
    figure.canvas.flush_events()

    print()
    print("KINETICS STARTED")
    print(f"Run ID:           {run_id}")
    print(f"Data file:        {csv_path.resolve()}")
    print("Press Ctrl+C to stop.")
    print()

    try:
        point_index = 0

        while True:

            if (
                maximum_points is not None
                and completed_points >= maximum_points
            ):
                stopped_reason = "maximum points reached"
                break

            target_start = run_zero + point_index * interval_seconds

            if not responsive_wait_until(
                target_start,
                figure,
                stop_state,
            ):
                if stop_state["requested"]:
                    stopped_reason = "stopped by user"
                else:
                    stopped_reason = "plot window closed"
                break

            if stop_state["requested"]:
                stopped_reason = "stopped by user"
                break

            acquisition_start_monotonic = time.monotonic()
            acquisition_start_wall = datetime.now().astimezone()
            acquisition_in_progress = True

            packet_number, counts = instrument.acquire_spectrum("d")

            acquisition_in_progress = False
            acquisition_end_monotonic = time.monotonic()
            acquisition_end_wall = datetime.now().astimezone()

            spectrum_df = make_spectrum_dataframe(counts)
            results_df = None

            if mode == "absorbance":
                results_df = calculate_sample_results(
                    session.blank_data,
                    spectrum_df,
                )
                selected_row = get_exact_wavelength_row(
                    results_df,
                    wavelength,
                )
                measured_value = float(selected_row["absorbance"])

                if not np.isfinite(measured_value):
                    raise ValueError(
                        f"Absorbance at {wavelength} nm is not finite."
                    )

                session.last_packet = packet_number
                session.last_data = spectrum_df.copy()
                session.record_latest_measurement(
                    measurement_type="s",
                    packet_number=packet_number,
                    dataframe=results_df,
                    label=f"{run_id} final",
                )
            else:
                selected_row = get_exact_wavelength_row(
                    spectrum_df,
                    wavelength,
                )
                measured_value = float(selected_row["counts"])
                session.store_transient(packet_number, spectrum_df)

            acquisition_start_seconds = (
                acquisition_start_monotonic - run_zero
            )
            acquisition_end_seconds = (
                acquisition_end_monotonic - run_zero
            )
            elapsed_seconds = (
                acquisition_start_seconds
                + acquisition_end_seconds
            ) / 2.0
            acquisition_seconds = (
                acquisition_end_monotonic
                - acquisition_start_monotonic
            )

            midpoint_timestamp = (
                acquisition_start_wall
                + (acquisition_end_wall - acquisition_start_wall) / 2
            ).isoformat(timespec="milliseconds")

            completed_points += 1
            elapsed_values.append(elapsed_seconds)
            measured_values.append(measured_value)

            rows = build_kinetics_rows(
                run_id=run_id,
                mode=mode,
                point_number=completed_points,
                selected_wavelength=wavelength,
                packet_number=packet_number,
                timestamp=midpoint_timestamp,
                scheduled_seconds=point_index * interval_seconds,
                acquisition_start_seconds=(
                    acquisition_start_seconds
                ),
                acquisition_end_seconds=acquisition_end_seconds,
                elapsed_seconds=elapsed_seconds,
                acquisition_seconds=acquisition_seconds,
                instrument_state=current_state,
                spectrum_df=spectrum_df,
                results_df=results_df,
            )

            append_kinetics_rows(csv_path, rows)

            update_kinetics_plot(
                figure=figure,
                axis=axis,
                line=line,
                status_text=status_text,
                elapsed_values=elapsed_values,
                measured_values=measured_values,
                mode=mode,
                wavelength=wavelength,
                point_number=completed_points,
                interval_seconds=interval_seconds,
                minor_grid_artists=minor_grid_artists,
            )

            print(
                f"Point {completed_points:4d}  "
                f"t={elapsed_seconds:9.3f} s  "
                f"{mode}={measured_value:.6g}  "
                f"acq={acquisition_seconds:.3f} s  "
                f"packet={packet_number}"
            )

            if (
                acquisition_seconds > interval_seconds
                and not warned_about_interval
            ):
                print(
                    "WARNING: Acquisition time exceeds the requested "
                    "interval. Measurements will run back-to-back."
                )
                warned_about_interval = True

            point_index += 1

            if stop_state["requested"]:
                stopped_reason = "stopped by user"
                print()
                break

    except KeyboardInterrupt:
        # Fallback for environments where SIGINT is delivered outside
        # the installed handler. Preserve the same graceful behavior.
        stopped_reason = "stopped by user"
        stop_state["requested"] = True
        print()

        if acquisition_in_progress:
            print(
                "Discarding the incomplete Pico data block and "
                "restoring serial synchronization..."
            )
            instrument.discard_interrupted_data_block()

    finally:
        signal.signal(signal.SIGINT, previous_sigint_handler)
        final_elapsed = time.monotonic() - run_zero

        if completed_points:
            if stopped_reason == "stopped by user":
                final_status = "Stopped by user"
            elif stopped_reason == "plot window closed":
                final_status = "Stopped: plot window closed"
            elif stopped_reason == "maximum points reached":
                final_status = "Completed: maximum points reached"
            else:
                final_status = stopped_reason.capitalize()

            status_text.set_text(
                f"{final_status}. "
                f"{completed_points} completed point(s)."
            )
            figure.canvas.draw()

            try:
                figure.savefig(png_path, dpi=150)

            except Exception as error:
                print(f"WARNING: Could not save plot: {error}")

        plt.close(figure)
        plt.ioff()

        print("Kinetic run finished.")
        print(f"Reason:            {stopped_reason}")
        print(f"Completed points:  {completed_points}")
        print(f"Elapsed time:      {final_elapsed:.3f} seconds")

        if csv_path.exists():
            print(f"Data saved:        {csv_path.resolve()}")

        if png_path.exists():
            print(f"Plot saved:        {png_path.resolve()}")

        print()


# ================================================================
# COMMAND-PARSING SUPPORT
# ================================================================

def get_command_argument(command, command_name):
    """Return the value following a command."""

    parts = command.split(maxsplit=1)

    if len(parts) != 2 or not parts[1].strip():
        raise ValueError(
            f"A value must follow {command_name}. "
            f"Example: {command_name} 100"
        )

    return parts[1].strip()


def clear_blank_if_setting_changed(
    session,
    setting_name,
    old_value,
    new_value,
):
    """Clear the stored blank only when a setting actually changed."""

    if old_value == new_value:
        return

    if session.invalidate_blank():
        print(
            f"Stored blank cleared because "
            f"{setting_name} changed."
        )


# ================================================================
# INTERACTIVE COMMAND CONSOLE
# ================================================================

def command_console(instrument, session):
    """Run the interactive Pico command console."""

    print("AS7343 instrument console")
    print("Enter a command or 'q' to quit.")
    print()
    print("Measurement commands:")
    print("  d                 transient raw spectrum")
    print("  b                 acquire and store blank")
    print("  s                 acquire sample with automatic ID")
    print("  id sample_name    override the next sample ID")
    print("  id                show the next sample ID")
    print("  samples           list stored samples")
    print("  p                 queue latest data for plotting")
    print("  p absorbance      queue selected sample column")
    print("  sp                show the completed overlay")
    print("  ks 640 10         absorbance kinetics; Ctrl+C stops")
    print("  ks 640 10 30      absorbance kinetics; max 30 points")
    print("  kd 640 10         raw-count kinetics; Ctrl+C stops")
    print("  kd 640 10 30      raw-count kinetics; max 30 points")
    print("  save file.csv     save without overwriting")
    print("  save! file.csv    save and replace existing file")
    print()
    print("Instrument commands:")
    print("  state")
    print("  gg")
    print("  sg 8x")
    print("  gt")
    print("  st 100")
    print("  gs")
    print("  ss 1000")
    print("  it")
    print("  grt")
    print("  srt 3000")
    print("  gsm")
    print()

    while True:

        try:
            command = input("Pico> ").strip()

        except (KeyboardInterrupt, EOFError):
            print()
            break

        if not command:
            continue

        command_lower = command.lower()

        if command_lower in ("q", "quit", "exit"):
            break

        try:

            # ----------------------------------------------------
            # Complete state and session status
            # ----------------------------------------------------

            if command_lower == "state":

                print()
                print_state(instrument.get_state())

                if session.blank_data is None:
                    print("Stored blank:       None")
                else:
                    print(
                        f"Stored blank:       "
                        f"{session.blank_packet}"
                    )

                next_sample_id = session.get_next_sample_id()

                latest = (
                    session.latest_measurement_label
                    if session.latest_measurement_label is not None
                    else "None"
                )

                print(f"Next sample ID:    {next_sample_id}")
                print(f"Stored samples:    {len(session.samples)}")
                print(f"Latest plot data:  {latest}")
                print(
                    f"Pending curves:     "
                    f"{len(session.pending_plot_series)}"
                )
                print()
                continue

            # ----------------------------------------------------
            # Sample ID
            # ----------------------------------------------------

            if command_lower == "id":

                print(
                    f"Next sample ID: "
                    f"{session.get_next_sample_id()}"
                )

                continue

            if command_lower.startswith("id "):

                sample_id = command.split(maxsplit=1)[1]
                session.set_pending_sample_id(sample_id)

                print(
                    f"Next sample ID: "
                    f"{session.pending_sample_id}"
                )
                continue

            # ----------------------------------------------------
            # List stored samples
            # ----------------------------------------------------

            if command_lower == "samples":

                if not session.samples:
                    print("No samples are currently stored.")
                    continue

                print("Stored samples:")

                for number, (
                    sample_id,
                    sample,
                ) in enumerate(
                    session.samples.items(),
                    start=1,
                ):
                    print(
                        f"  {number:2d}. {sample_id}  "
                        f"({sample['packet']})"
                    )

                continue

            # ----------------------------------------------------
            # Live kinetics
            # ----------------------------------------------------

            if (
                command_lower in ("ks", "kd")
                or command_lower.startswith("ks ")
                or command_lower.startswith("kd ")
            ):

                kinetics_settings = parse_kinetics_command(command)

                run_kinetics(
                    instrument,
                    session,
                    kinetics_settings,
                )

                continue

            # ----------------------------------------------------
            # Show pending overlay
            # ----------------------------------------------------

            if command_lower == "sp":

                number_shown = show_pending_plot(session)

                print(
                    f"Displayed {number_shown} curve(s)."
                )

                continue

            # ----------------------------------------------------
            # Add latest measurement to pending overlay
            # ----------------------------------------------------

            if (
                command_lower == "p"
                or command_lower.startswith("p ")
            ):

                if command_lower == "p":
                    requested_column = None
                else:
                    requested_column = command.split(
                        maxsplit=1
                    )[1].strip()

                plot_status = add_latest_measurement_to_plot(
                    session,
                    requested_column,
                )

                print(
                    f"Added {plot_status['label']}: "
                    f"{plot_status['column']} "
                    f"({plot_status['points']} points)"
                )

                print(
                    f"Pending overlay curves: "
                    f"{plot_status['pending']}"
                )

                continue

            # ----------------------------------------------------
            # Save experiment CSV
            # ----------------------------------------------------

            if (
                command_lower == "save"
                or command_lower.startswith("save ")
            ):

                filename = get_command_argument(
                    command,
                    "save",
                )

                path, row_count = save_experiment_csv(
                    session,
                    filename,
                    overwrite=False,
                )

                print(
                    f"Saved {row_count} rows to: "
                    f"{path.resolve()}"
                )
                continue

            if (
                command_lower == "save!"
                or command_lower.startswith("save! ")
            ):

                filename = get_command_argument(
                    command,
                    "save!",
                )

                path, row_count = save_experiment_csv(
                    session,
                    filename,
                    overwrite=True,
                )

                print(
                    f"Saved {row_count} rows to: "
                    f"{path.resolve()}"
                )
                continue

            # ----------------------------------------------------
            # Transient raw spectrum
            # ----------------------------------------------------

            if command_lower == "d":

                packet_number, counts = (
                    instrument.acquire_spectrum("d")
                )

                dataframe = make_spectrum_dataframe(counts)

                session.store_transient(
                    packet_number,
                    dataframe,
                )

                print("Data block OK")
                print(f"Packet: {packet_number}")
                print()
                print_raw_spectrum(dataframe)
                print()

                continue

            # ----------------------------------------------------
            # Blank
            # ----------------------------------------------------

            if command_lower == "b":

                packet_number, counts = (
                    instrument.acquire_spectrum("d")
                )

                blank_df = make_spectrum_dataframe(counts)

                blank_state = capture_measurement_state(
                    instrument
                )

                old_sample_count = len(session.samples)

                session.store_blank(
                    packet_number,
                    blank_df,
                    blank_state,
                )

                print("Blank data block OK")
                print(f"Blank stored from: {packet_number}")

                if old_sample_count:
                    print(
                        f"Cleared {old_sample_count} sample(s) "
                        f"that used the previous blank."
                    )

                print()
                print_raw_spectrum(blank_df)
                print()

                continue

            # ----------------------------------------------------
            # Sample
            # ----------------------------------------------------

            if command_lower == "s":

                if session.blank_data is None:

                    print(
                        "You gotta have a blank; otherwise, "
                        "you don't gotta sample!"
                    )
                    continue

                sample_id = session.get_next_sample_id()

                if sample_id in session.samples:
                    raise ValueError(
                        f"Sample ID {sample_id!r} already exists."
                    )

                current_state = capture_measurement_state(
                    instrument
                )

                if not measurement_states_match(
                    session.blank_state,
                    current_state,
                ):

                    session.invalidate_blank()

                    print(
                        "Instrument settings have changed "
                        "since the blank was acquired."
                    )
                    print(
                        "The old blank and its samples have "
                        "been cleared. Acquire a new blank."
                    )
                    continue

                packet_number, counts = (
                    instrument.acquire_spectrum("d")
                )

                sample_df = make_spectrum_dataframe(counts)

                results = calculate_sample_results(
                    session.blank_data,
                    sample_df,
                )

                session.store_sample(
                    sample_id,
                    packet_number,
                    sample_df,
                    results,
                    current_state,
                )

                print("Sample data block OK")
                print(f"Sample ID:     {sample_id}")
                print(f"Sample packet: {packet_number}")
                print(f"Blank packet:  {session.blank_packet}")
                print()

                print_sample_results(results)
                print()

                continue

            # ----------------------------------------------------
            # Gain
            # ----------------------------------------------------

            if command_lower == GET_GAIN_COMMAND:

                gain = instrument.get_gain()
                print(f"Gain: {gain}")
                continue

            if (
                command_lower == SET_GAIN_COMMAND
                or command_lower.startswith(
                    SET_GAIN_COMMAND + " "
                )
            ):

                target = get_command_argument(
                    command_lower,
                    SET_GAIN_COMMAND,
                )

                old_gain = instrument.get_gain()
                new_gain = instrument.set_gain(target)

                print(f"Gain set to: {new_gain}")

                clear_blank_if_setting_changed(
                    session,
                    "gain",
                    old_gain,
                    new_gain,
                )

                continue

            # ----------------------------------------------------
            # ATIME
            # ----------------------------------------------------

            if command_lower == GET_ATIME_COMMAND:

                atime = instrument.get_atime()
                print(f"ATIME: {atime}")
                continue

            if (
                command_lower == SET_ATIME_COMMAND
                or command_lower.startswith(
                    SET_ATIME_COMMAND + " "
                )
            ):

                value = get_command_argument(
                    command_lower,
                    SET_ATIME_COMMAND,
                )

                old_atime = instrument.get_atime()
                new_atime = instrument.set_atime(value)

                print(f"ATIME set to: {new_atime}")
                print_timing_state(instrument)

                clear_blank_if_setting_changed(
                    session,
                    "ATIME",
                    old_atime,
                    new_atime,
                )

                continue

            # ----------------------------------------------------
            # ASTEP
            # ----------------------------------------------------

            if command_lower == GET_ASTEP_COMMAND:

                astep = instrument.get_astep()
                print(f"ASTEP: {astep}")
                continue

            if (
                command_lower == SET_ASTEP_COMMAND
                or command_lower.startswith(
                    SET_ASTEP_COMMAND + " "
                )
            ):

                value = get_command_argument(
                    command_lower,
                    SET_ASTEP_COMMAND,
                )

                old_astep = instrument.get_astep()
                new_astep = instrument.set_astep(value)

                print(f"ASTEP set to: {new_astep}")
                print_timing_state(instrument)

                clear_blank_if_setting_changed(
                    session,
                    "ASTEP",
                    old_astep,
                    new_astep,
                )

                continue

            # ----------------------------------------------------
            # Integration time
            # ----------------------------------------------------

            if command_lower == GET_INTEGRATION_TIME_COMMAND:

                integration_time = (
                    instrument.get_integration_time()
                )

                print(
                    f"Integration time: "
                    f"{integration_time} ms"
                )
                continue

            # ----------------------------------------------------
            # Pico read timeout
            # ----------------------------------------------------

            if command_lower == GET_READ_TIMEOUT_COMMAND:

                timeout_ms = instrument.get_read_timeout()
                print(f"Read timeout: {timeout_ms} ms")
                continue

            if (
                command_lower == SET_READ_TIMEOUT_COMMAND
                or command_lower.startswith(
                    SET_READ_TIMEOUT_COMMAND + " "
                )
            ):

                value = get_command_argument(
                    command_lower,
                    SET_READ_TIMEOUT_COMMAND,
                )

                timeout_ms = instrument.set_read_timeout(value)
                print(f"Read timeout set to: {timeout_ms} ms")
                continue

            # ----------------------------------------------------
            # SMUX mode
            # ----------------------------------------------------

            if command_lower == GET_SMUX_MODE_COMMAND:

                channels = instrument.get_smux_channels()
                cycles = instrument.smux_cycles_from_channels(
                    channels
                )
                print(
                    f"SMUX mode: {channels} channels "
                    f"({cycles} cycle{'s' if cycles != 1 else ''})"
                )
                continue

            print(f"Unknown command: {command}")

        except Exception as error:
            print(f"ERROR: {error}")


def main():
    """Connect to the Pico and start the command console."""

    instrument = None

    try:
        instrument = PicoInstrument(port=SERIAL_PORT)
        instrument.connect()

        session = MeasurementSession()

        initial_state = instrument.get_state()

        if initial_state["SMUX channels"] != 18:
            raise RuntimeError(
                "The Ubuntu UI requires Pico SMUX mode CH18 "
                "(18 returned values)."
            )

        print_state(initial_state)
        command_console(instrument, session)

    except Exception as error:
        print(f"ERROR: {error}")
        print()
        list_serial_ports()

    finally:
        if instrument is not None and instrument.connected:
            instrument.close()


if __name__ == "__main__":
    main()
