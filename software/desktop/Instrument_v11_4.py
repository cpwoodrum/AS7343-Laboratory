"""
Instrument_v11_4.py

AS7343 Ubuntu user-interface update built on Instrument_v11_3.py.

Version 11.4 additions
----------------------
- Adjustable spectral display range from 400 through 900 nm.
- PCHIP interpolation remains confined to measured AS7343 wavelengths;
  changing the display range never extrapolates unsupported values.
- Selectable output directory for manual CSV saves and automatic kinetics
  CSV/PNG files.
- Persistent UI settings stored in ~/.config/as7343_ui/settings.json.
- Convenience command for dated experiment folders:
      outdir today Blue1_Kinetics
  selects:
      /home/Data/AS7343/Experiments/Blue1_Kinetics/YYYY-MM-DD/Data
- Kinetics remains in the terminal at a visible Pico> prompt until ENTER
  is pressed. The Matplotlib window is created only after the start signal,
  and experimental time zero is established after the window is ready.

Installation
------------
Keep Instrument_v11_4.py in the same directory as Instrument_v11_3.py,
then run Instrument_v11_4.py with Thonny's local Python interpreter.
AS7343_Pico_v3.py is unchanged.
"""

from __future__ import annotations

import builtins
import importlib.util
import json
import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Any


# =====================================================================
# BASE PROGRAM LOADING
# =====================================================================

BASE_FILENAME = "Instrument_v11_3.py"
SCRIPT_DIRECTORY = Path(__file__).resolve().parent


def find_base_program() -> Path:
    """Locate Instrument_v11_3.py beside this file or in the CWD."""

    candidates = [
        SCRIPT_DIRECTORY / BASE_FILENAME,
        Path.cwd() / BASE_FILENAME,
    ]

    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()

    checked = "\n".join(f"  {path}" for path in candidates)
    raise FileNotFoundError(
        f"Could not find {BASE_FILENAME}. Keep it in the same directory "
        f"as Instrument_v11_4.py.\nChecked:\n{checked}"
    )


def load_base_program(path: Path) -> ModuleType:
    """Import the v11.3 program without running its main() function."""

    spec = importlib.util.spec_from_file_location(
        "_as7343_instrument_v11_3",
        path,
    )

    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load base program: {path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


BASE_PATH = find_base_program()
base = load_base_program(BASE_PATH)
ORIGINAL_INPUT = builtins.input


# =====================================================================
# PERSISTENT UI SETTINGS
# =====================================================================

HARD_PLOT_MIN_NM = 400
HARD_PLOT_MAX_NM = 900
DEFAULT_PLOT_MIN_NM = 400
DEFAULT_PLOT_MAX_NM = 900

DEFAULT_OUTPUT_DIRECTORY = Path("/home/Data/AS7343/Experiments")
CONFIG_DIRECTORY = Path.home() / ".config" / "as7343_ui"
CONFIG_PATH = CONFIG_DIRECTORY / "settings.json"


class UISettings:
    """Persistent settings that belong to the Ubuntu UI, not the Pico."""

    def __init__(self) -> None:
        self.plot_min_nm = DEFAULT_PLOT_MIN_NM
        self.plot_max_nm = DEFAULT_PLOT_MAX_NM
        self.output_directory = DEFAULT_OUTPUT_DIRECTORY
        self.load()
        self.ensure_output_directory()

    def load(self) -> None:
        """Load valid saved settings; retain defaults on any problem."""

        if not CONFIG_PATH.is_file():
            return

        try:
            payload = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

            lower = int(payload.get("plot_min_nm", DEFAULT_PLOT_MIN_NM))
            upper = int(payload.get("plot_max_nm", DEFAULT_PLOT_MAX_NM))
            self.validate_plot_range(lower, upper)

            raw_directory = payload.get(
                "output_directory",
                str(DEFAULT_OUTPUT_DIRECTORY),
            )
            directory = Path(str(raw_directory)).expanduser()

            self.plot_min_nm = lower
            self.plot_max_nm = upper
            self.output_directory = directory

        except Exception as error:
            print(
                "WARNING: UI settings could not be read; using defaults: "
                f"{error}"
            )

    def save(self) -> None:
        """Write settings atomically to the user's configuration folder."""

        CONFIG_DIRECTORY.mkdir(parents=True, exist_ok=True)

        payload = {
            "plot_min_nm": self.plot_min_nm,
            "plot_max_nm": self.plot_max_nm,
            "output_directory": str(self.output_directory),
        }

        temporary_path = CONFIG_PATH.with_suffix(".json.temporary")
        temporary_path.write_text(
            json.dumps(payload, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary_path.replace(CONFIG_PATH)

    @staticmethod
    def validate_plot_range(lower: int, upper: int) -> None:
        """Validate a requested display range."""

        if lower < HARD_PLOT_MIN_NM or upper > HARD_PLOT_MAX_NM:
            raise ValueError(
                f"Plot limits must remain within "
                f"{HARD_PLOT_MIN_NM} to {HARD_PLOT_MAX_NM} nm."
            )

        if lower >= upper:
            raise ValueError(
                "The lower plot limit must be less than the upper limit."
            )

    def set_plot_range(self, lower: int, upper: int) -> None:
        self.validate_plot_range(lower, upper)
        self.plot_min_nm = int(lower)
        self.plot_max_nm = int(upper)
        self.save()

    def reset_plot_range(self) -> None:
        self.set_plot_range(DEFAULT_PLOT_MIN_NM, DEFAULT_PLOT_MAX_NM)

    def ensure_output_directory(self) -> None:
        """Create the output directory, falling back safely if necessary."""

        try:
            self.output_directory.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            fallback = Path.cwd().resolve()
            print(
                "WARNING: Could not create output directory "
                f"{self.output_directory}: {error}"
            )
            print(f"Using current directory instead: {fallback}")
            self.output_directory = fallback

    def set_output_directory(self, directory: Path) -> None:
        directory = directory.expanduser()

        if not directory.is_absolute():
            directory = (Path.cwd() / directory).resolve()

        directory.mkdir(parents=True, exist_ok=True)

        if not directory.is_dir():
            raise NotADirectoryError(f"Not a directory: {directory}")

        self.output_directory = directory.resolve()
        self.save()

    def reset_output_directory(self) -> None:
        self.set_output_directory(DEFAULT_OUTPUT_DIRECTORY)

    def set_daily_experiment_directory(self, experiment_name: str) -> None:
        """Select Experiment/YYYY-MM-DD/Data under the standard hierarchy."""

        cleaned = experiment_name.strip().replace(" ", "_")

        if not cleaned:
            raise ValueError(
                "An experiment name is required. "
                "Example: outdir today Blue1_Kinetics"
            )

        if cleaned in (".", "..") or "/" in cleaned or "\\" in cleaned:
            raise ValueError(
                "Use a simple experiment folder name without slashes."
            )

        dated_directory = (
            DEFAULT_OUTPUT_DIRECTORY
            / cleaned
            / datetime.now().strftime("%Y-%m-%d")
            / "Data"
        )
        self.set_output_directory(dated_directory)


ui = UISettings()


# =====================================================================
# COMMAND SUPPORT FOR UI SETTINGS
# =====================================================================

_print_ui_state_before_next_prompt = False


def print_ui_settings() -> None:
    """Display settings controlled by Instrument_v11_4.py."""

    print(f"Plot range:         {ui.plot_min_nm} to {ui.plot_max_nm} nm")
    print(f"Output directory:   {ui.output_directory}")


def print_v11_4_commands() -> None:
    """Display the additional command syntax."""

    print("Version 11.4 UI commands:")
    print("  range                         show plot wavelength range")
    print("  range 400 900                 set plot wavelength range")
    print("  range default                 reset plot range to 400-900 nm")
    print("  outdir                        show output directory")
    print("  outdir /path/to/folder        set/create output directory")
    print("  outdir default                reset to /home/Data/AS7343/Experiments")
    print("  outdir today Experiment_Name  select today's experiment Data folder")
    print("  uistate                       show plot range and output directory")
    print()


def handle_range_command(command: str) -> bool:
    """Handle a range command. Return True when it was recognized."""

    parts = command.split()

    if not parts or parts[0].lower() != "range":
        return False

    if len(parts) == 1:
        print(f"Plot range: {ui.plot_min_nm} to {ui.plot_max_nm} nm")
        return True

    if len(parts) == 2 and parts[1].lower() == "default":
        ui.reset_plot_range()
        print(
            f"Plot range reset to: "
            f"{ui.plot_min_nm} to {ui.plot_max_nm} nm"
        )
        return True

    if len(parts) != 3:
        raise ValueError(
            "Use: range, range lower upper, or range default"
        )

    try:
        lower = int(parts[1])
        upper = int(parts[2])
    except ValueError as error:
        raise ValueError("Plot limits must be integer wavelengths.") from error

    ui.set_plot_range(lower, upper)
    print(f"Plot range set to: {lower} to {upper} nm")
    return True


def handle_outdir_command(command: str) -> bool:
    """Handle an outdir command. Return True when it was recognized."""

    stripped = command.strip()
    parts = stripped.split(maxsplit=2)

    if not parts or parts[0].lower() != "outdir":
        return False

    if len(parts) == 1:
        print(f"Output directory: {ui.output_directory}")
        return True

    if parts[1].lower() == "default" and len(parts) == 2:
        ui.reset_output_directory()
        print(f"Output directory reset to: {ui.output_directory}")
        return True

    if parts[1].lower() == "today":
        if len(parts) != 3:
            raise ValueError(
                "Use: outdir today Experiment_Name"
            )
        ui.set_daily_experiment_directory(parts[2])
        print(f"Today's output directory set to: {ui.output_directory}")
        return True

    path_text = stripped.split(maxsplit=1)[1]
    ui.set_output_directory(Path(path_text))
    print(f"Output directory set to: {ui.output_directory}")
    return True


def enhanced_input(prompt: str = "") -> str:
    """Intercept v11.4 UI commands at the existing Pico> prompt."""

    global _print_ui_state_before_next_prompt

    # Other input() calls should behave normally. run_kinetics() is replaced
    # below and deliberately uses ORIGINAL_INPUT for its start prompt.
    is_console_prompt = prompt.strip().lower().startswith("pico>")

    if not is_console_prompt:
        return ORIGINAL_INPUT(prompt)

    while True:
        if _print_ui_state_before_next_prompt:
            print_ui_settings()
            print()
            _print_ui_state_before_next_prompt = False

        command = ORIGINAL_INPUT(prompt)
        command_lower = command.strip().lower()

        try:
            if handle_range_command(command):
                continue

            if handle_outdir_command(command):
                continue

            if command_lower in ("uistate", "ui"):
                print_ui_settings()
                continue

            if command_lower in ("help", "h", "?"):
                print_v11_4_commands()
                continue

            if command_lower == "state":
                # Let v11.3 print the instrument/session state. The additional
                # v11.4 state is printed just before the next Pico> prompt.
                _print_ui_state_before_next_prompt = True

            return command

        except Exception as error:
            print(f"ERROR: {error}")


# The existing command console resolves input from its module globals.
base.input = enhanced_input


# =====================================================================
# OUTPUT-PATH PATCHES
# =====================================================================


def normalize_csv_path(filename: str) -> Path:
    """Resolve relative experiment filenames beneath the selected outdir."""

    filename = filename.strip()

    if not filename:
        raise ValueError("CSV filename cannot be empty.")

    path = Path(filename).expanduser()

    if path.suffix == "":
        path = path.with_suffix(".csv")

    if path.suffix.lower() != ".csv":
        raise ValueError("Experiment files must use the .csv extension.")

    if not path.is_absolute():
        path = ui.output_directory / path

    return path


def unique_kinetics_paths(mode: str, wavelength: int) -> tuple[Path, Path]:
    """Return unused kinetics CSV and PNG paths in the selected outdir."""

    ui.ensure_output_directory()
    directory = ui.output_directory
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = f"kinetics_{timestamp}_{mode}_{wavelength}nm"
    csv_path = directory / f"{stem}.csv"
    png_path = directory / f"{stem}.png"
    suffix = 2

    while csv_path.exists() or png_path.exists():
        csv_path = directory / f"{stem}_{suffix}.csv"
        png_path = directory / f"{stem}_{suffix}.png"
        suffix += 1

    return csv_path, png_path


base.normalize_csv_path = normalize_csv_path
base.unique_kinetics_paths = unique_kinetics_paths


# =====================================================================
# SPECTRAL-PLOT PATCHES
# =====================================================================


def add_latest_measurement_to_plot(
    session: Any,
    requested_column: str | None = None,
) -> dict[str, Any]:
    """
    Queue all measured spectral points, independent of display limits.

    The currently selected wavelength range is applied only by sp. This lets
    the operator change the range after several curves have been queued.
    """

    source_column, plot_kind = base.resolve_plot_column(
        session,
        requested_column,
    )

    if (
        session.pending_plot_kind is not None
        and session.pending_plot_kind != plot_kind
    ):
        raise ValueError(
            f"The pending overlay contains {session.pending_plot_kind} data. "
            f"Cannot add {plot_kind} data to the same plot. Use sp to "
            "show the current overlay first."
        )

    dataframe = session.latest_plot_data

    # Retain the complete measured sensor range for later display choices.
    # The broad 555 nm FY channel remains excluded from plotted curves only.
    plot_mask = (
        dataframe["wavelength"].between(405, 855)
        & ~dataframe["wavelength"].isin(base.EXCLUDED_PLOT_WAVELENGTHS)
    )

    plot_df = dataframe.loc[
        plot_mask,
        ["wavelength", source_column],
    ].copy()

    plot_df[source_column] = base.pd.to_numeric(
        plot_df[source_column],
        errors="coerce",
    )
    plot_df = plot_df.replace([base.np.inf, -base.np.inf], base.np.nan)
    plot_df = plot_df.dropna(subset=["wavelength", source_column])
    plot_df = plot_df.sort_values(by="wavelength").reset_index(drop=True)

    if plot_df.empty:
        raise ValueError(
            f"Column {source_column!r} contains no finite measured values."
        )

    session.pending_plot_series.append(
        {
            "wavelength": plot_df["wavelength"].to_numpy(copy=True),
            "values": plot_df[source_column].to_numpy(copy=True),
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


def build_pchip_curve(
    wavelengths: Any,
    values: Any,
) -> tuple[Any, Any]:
    """Build a 1 nm PCHIP curve without extrapolating measured support."""

    x_measured = base.np.asarray(wavelengths, dtype=float)
    y_measured = base.np.asarray(values, dtype=float)

    if x_measured.size < 2:
        return base.np.array([]), base.np.array([])

    order = base.np.argsort(x_measured)
    x_measured = x_measured[order]
    y_measured = y_measured[order]

    curve_min = max(float(x_measured.min()), float(ui.plot_min_nm))
    curve_max = min(float(x_measured.max()), float(ui.plot_max_nm))

    if curve_min > curve_max:
        return base.np.array([]), base.np.array([])

    interpolator = base.PchipInterpolator(
        x_measured,
        y_measured,
        extrapolate=False,
    )

    x_smooth = base.np.arange(
        curve_min,
        curve_max + base.PCHIP_STEP_NM,
        base.PCHIP_STEP_NM,
        dtype=float,
    )
    y_smooth = interpolator(x_smooth)
    valid = base.np.isfinite(y_smooth)
    return x_smooth[valid], y_smooth[valid]


def show_pending_plot(session: Any) -> int:
    """Display the queued overlay using the current v11.4 plot range."""

    if not session.pending_plot_series:
        raise ValueError(
            "There are no pending curves to show. Use p after a "
            "d, b, or s measurement first."
        )

    plot_kind = session.pending_plot_kind
    y_axis_label = base.format_plot_axis_label(plot_kind)
    figure, axis = base.plt.subplots(figsize=(9, 5.5))
    visible_content = False

    for series in session.pending_plot_series:
        wavelengths = base.np.asarray(series["wavelength"], dtype=float)
        values = base.np.asarray(series["values"], dtype=float)
        marker_mask = (
            (wavelengths >= ui.plot_min_nm)
            & (wavelengths <= ui.plot_max_nm)
        )

        x_smooth, y_smooth = build_pchip_curve(wavelengths, values)

        if x_smooth.size:
            smooth_line, = axis.plot(
                x_smooth,
                y_smooth,
                linestyle="-",
                linewidth=1.5,
                zorder=3,
                label=series["label"],
            )
            line_color = smooth_line.get_color()
            visible_content = True
        elif marker_mask.any():
            marker_line, = axis.plot(
                wavelengths[marker_mask],
                values[marker_mask],
                linestyle="none",
                marker="o",
                zorder=4,
                label=series["label"],
            )
            line_color = marker_line.get_color()
            visible_content = True
        else:
            continue

        if marker_mask.any():
            axis.plot(
                wavelengths[marker_mask],
                values[marker_mask],
                linestyle="none",
                marker="o",
                color=line_color,
                zorder=4,
                label="_nolegend_",
            )

    if not visible_content:
        base.plt.close(figure)
        raise ValueError(
            "The selected plot range contains no measured or interpolated "
            "spectral values. Change the range and try sp again."
        )

    axis.set_xlim(ui.plot_min_nm, ui.plot_max_nm)
    axis.minorticks_on()
    axis.xaxis.set_major_locator(base.MultipleLocator(50))
    axis.xaxis.set_minor_locator(base.MultipleLocator(10))
    axis.yaxis.set_minor_locator(base.AutoMinorLocator(2))
    axis.set_xlabel("Wavelength (nm)")
    axis.set_ylabel(y_axis_label)
    axis.set_title(
        f"AS7343 {y_axis_label} Overlay "
        f"(PCHIP, 1 nm; display {ui.plot_min_nm}-{ui.plot_max_nm} nm)"
    )
    axis.set_axisbelow(True)
    axis.grid(
        visible=True,
        which="major",
        color="0.62",
        linewidth=0.8,
        zorder=1,
    )
    axis.grid(
        visible=True,
        which="minor",
        color="0.82",
        linewidth=0.5,
        zorder=1,
    )
    axis.tick_params(axis="x", which="minor", length=4)
    axis.tick_params(axis="y", which="minor", length=3)
    axis.legend()
    figure.tight_layout()

    number_shown = len(session.pending_plot_series)
    figure.canvas.draw()
    base.plt.show(block=True)

    session.pending_plot_series.clear()
    session.pending_plot_kind = None
    return number_shown


base.add_latest_measurement_to_plot = add_latest_measurement_to_plot
base.build_pchip_curve = build_pchip_curve
base.show_pending_plot = show_pending_plot


# =====================================================================
# KINETICS START-PROMPT PATCH
# =====================================================================


def run_kinetics(instrument: Any, session: Any, settings: dict[str, Any]) -> None:
    """Run kinetics while retaining terminal focus until ENTER is pressed."""

    mode = settings["mode"]
    wavelength = settings["wavelength"]
    interval_seconds = settings["interval_seconds"]
    maximum_points = settings["maximum_points"]

    if mode == "absorbance":
        if session.blank_data is None:
            raise ValueError("Absorbance kinetics requires a stored blank.")

        current_state = base.capture_measurement_state(instrument)

        if not base.measurement_states_match(
            session.blank_state,
            current_state,
        ):
            session.invalidate_blank()
            raise ValueError(
                "Instrument settings changed after the blank. "
                "The old blank was cleared; acquire a new blank."
            )
    else:
        current_state = base.capture_measurement_state(instrument)

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

    if wavelength not in base.VALIDATED_WHITE_LED_WAVELENGTHS:
        print(
            "WARNING: This wavelength is outside the validated "
            "405-640 nm white-LED range."
        )

    print(f"Output directory: {ui.output_directory}")
    print()
    print("Press ENTER at the Pico> prompt to start.")
    print("Press Ctrl+C to cancel before starting.")

    try:
        while True:
            start_text = ORIGINAL_INPUT("Pico> ")
            if not start_text.strip():
                break
            print("Press ENTER without typing a command to start kinetics.")

    except KeyboardInterrupt:
        print()
        print("Kinetic run cancelled before start.")
        return

    run_id = datetime.now().strftime("run_%Y%m%d_%H%M%S")
    csv_path, png_path = unique_kinetics_paths(mode, wavelength)

    # Only now create/show the figure. The terminal therefore retains focus
    # throughout the armed waiting period. Figure-creation time is excluded
    # from experimental elapsed time.
    base.plt.ion()
    figure, axis = base.plt.subplots(figsize=(9, 5.5))

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
    axis.set_title(f"AS7343 {mode.title()} Kinetics at {wavelength} nm")
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
        "Waiting for first point...",
        transform=axis.transAxes,
        verticalalignment="top",
    )
    minor_grid_artists: list[Any] = []
    figure.tight_layout()
    figure.canvas.draw()
    base.plt.show(block=False)
    base.plt.pause(0.05)

    elapsed_values: list[float] = []
    measured_values: list[float] = []
    completed_points = 0
    acquisition_in_progress = False
    stopped_reason = "completed"
    warned_about_interval = False

    stop_state = {"requested": False}
    previous_sigint_handler = signal.getsignal(signal.SIGINT)

    def request_kinetics_stop(signum: int, frame: Any) -> None:
        stop_state["requested"] = True

    signal.signal(signal.SIGINT, request_kinetics_stop)

    print()
    print("KINETICS STARTED")
    print(f"Run ID:           {run_id}")
    print(f"Data file:        {csv_path.resolve()}")
    print("Press Ctrl+C to stop.")
    print()

    # Establish time zero only after the live window is ready and the start
    # messages have been printed.
    run_zero = time.monotonic()

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

            if not base.responsive_wait_until(
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

            spectrum_df = base.make_spectrum_dataframe(counts)
            results_df = None

            if mode == "absorbance":
                results_df = base.calculate_sample_results(
                    session.blank_data,
                    spectrum_df,
                )
                selected_row = base.get_exact_wavelength_row(
                    results_df,
                    wavelength,
                )
                measured_value = float(selected_row["absorbance"])

                if not base.np.isfinite(measured_value):
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
                selected_row = base.get_exact_wavelength_row(
                    spectrum_df,
                    wavelength,
                )
                measured_value = float(selected_row["counts"])
                session.store_transient(packet_number, spectrum_df)

            acquisition_start_seconds = (
                acquisition_start_monotonic - run_zero
            )
            acquisition_end_seconds = acquisition_end_monotonic - run_zero
            elapsed_seconds = (
                acquisition_start_seconds + acquisition_end_seconds
            ) / 2.0
            acquisition_seconds = (
                acquisition_end_monotonic - acquisition_start_monotonic
            )

            midpoint_timestamp = (
                acquisition_start_wall
                + (acquisition_end_wall - acquisition_start_wall) / 2
            ).isoformat(timespec="milliseconds")

            completed_points += 1
            elapsed_values.append(elapsed_seconds)
            measured_values.append(measured_value)

            rows = base.build_kinetics_rows(
                run_id=run_id,
                mode=mode,
                point_number=completed_points,
                selected_wavelength=wavelength,
                packet_number=packet_number,
                timestamp=midpoint_timestamp,
                scheduled_seconds=point_index * interval_seconds,
                acquisition_start_seconds=acquisition_start_seconds,
                acquisition_end_seconds=acquisition_end_seconds,
                elapsed_seconds=elapsed_seconds,
                acquisition_seconds=acquisition_seconds,
                instrument_state=current_state,
                spectrum_df=spectrum_df,
                results_df=results_df,
            )
            base.append_kinetics_rows(csv_path, rows)

            base.update_kinetics_plot(
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
                f"{final_status}. {completed_points} completed point(s)."
            )
            figure.canvas.draw()

            try:
                figure.savefig(png_path, dpi=150)
            except Exception as error:
                print(f"WARNING: Could not save plot: {error}")

        base.plt.close(figure)
        base.plt.ioff()

        print("Kinetic run finished.")
        print(f"Reason:            {stopped_reason}")
        print(f"Completed points:  {completed_points}")
        print(f"Elapsed time:      {final_elapsed:.3f} seconds")

        if csv_path.exists():
            print(f"Data saved:        {csv_path.resolve()}")

        if png_path.exists():
            print(f"Plot saved:        {png_path.resolve()}")

        print()


base.run_kinetics = run_kinetics


# =====================================================================
# STARTUP DISPLAY AND MAIN
# =====================================================================

_original_command_console = base.command_console


def command_console(instrument: Any, session: Any) -> None:
    """Add the v11.4 command summary, then use the proven v11.3 console."""

    print("Instrument_v11_4 UI additions are active.")
    print_ui_settings()
    print_v11_4_commands()
    _original_command_console(instrument, session)


base.command_console = command_console


def main() -> None:
    """Run the patched v11.3 program as Instrument_v11_4."""

    base.main()


if __name__ == "__main__":
    main()
