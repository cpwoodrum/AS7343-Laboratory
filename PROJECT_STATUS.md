# AS7343 Laboratory Project Status

**Bartel Technologies**  
*Measure Carefully. Think Deeply.*

## Current Software Baselines

### Raspberry Pi Pico firmware

- `firmware/pico/Code.py`
- Current saved and tested Pico firmware baseline
- Documented as compatible with `software/desktop/Instrument_v11_4.py`

### Desktop software

- `software/desktop/Instrument_v11_3.py`
- `software/desktop/Instrument_v11_4.py`
- Current saved working desktop baseline: `Instrument_v11_4.py`

`Instrument_v11_4_1.py`, used for later nitrate-study measurements, has not yet been added to the repository.

## Current Report Work

### Copper(II)-Ammonia Calibration and Recovery Study

Current reviewed master:

- `BT_Cu_NH4OH_Calibration_Recovery_Report_Draft_v0_4.docx`

Status:

- Draft v0.4 reconciles the complete v0.2 report with the completed three-block cuvette remove-and-reinsert study.
- Draft v0.3 was an incomplete reconstruction and is not the report master.
- Draft v0.4 is awaiting final detailed review before repository publication.

## Repository Rules

1. Pull the repository before beginning work.
2. Preserve validated software versions rather than overwriting them.
3. Retain raw experimental data unchanged.
4. Use the latest approved report document as the canonical master.
5. Do not reconstruct a report from chat history when the master document is available.
6. Commit and push before changing computers.
7. Record important baseline changes in this file.

## Pending Work

- Add and document `Instrument_v11_4_1.py`.
- Confirm whether the existing Pico `Code.py` was used unchanged with v11.4.1.
- Add the copper-ammonia raw data and supporting metadata.
- Publish the copper-ammonia report after final review.
- Revise the root README description of nitrate-analysis capability.
- Continue development of the laboratory manual, technical reports, and technical notes.
