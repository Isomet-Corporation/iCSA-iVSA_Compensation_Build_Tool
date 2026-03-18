# iCSA / iVSA Compensation Build Tool

![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)
![Python: 3.14+](https://img.shields.io/badge/Python-3.14-blue.svg)
![Platform: Windows-blue](https://img.shields.io/badge/Platform-Windows-blue.svg)

A Python GUI application for generating, tuning, previewing, exporting, and storing compensation LUTs for compatible **Isomet iCSA / iVSA systems** using the `imslib` SDK.

The main application file is:

```text
Compensation_tool.py
```

This tool is intended to support the full compensation workflow, from generating a starting LUT through to downloading and storing it on the synthesiser.

---

## Features

### Compensation LUT generation

The application can generate a starting LUT based on:

* minimum frequency
* maximum frequency
* number of points
* starting amplitude
* optional AO device model
* optional optical wavelength

This provides a quick first-pass compensation table for further tuning.

### Interactive LUT tuning

The main tuning interface allows you to:

* edit LUT points directly
* adjust **amplitude** and **phase** for the selected point
* preview changes immediately on the graph
* apply the selected point as a live calibration tone
* control RF output settings while tuning

Each LUT point contains:

* frequency
* amplitude
* phase

### Rendered LUT preview

The graph shows the LUT as rendered through the SDK compensation functions, rather than just plotting the raw control points.

Supported interpolation styles include:

* Spot
* Step
* Linear
* Linear Extend
* B-Spline

Amplitude and phase interpolation can be selected independently.

### RF drive controls

The tuning tab includes controls for:

* DDS amplitude
* Wiper 1 amplitude
* Wiper 2 amplitude
* amplifier enable
* channel wiper sync
* stop tone

These controls are useful during interactive compensation setup and testing.

### Save and store workflow

Once tuning is complete, the application can:

* save the LUT to a `.lut` file
* download the compensation table to the synthesiser
* verify the download
* store the table as the synthesiser **default startup LUT**

### Help overlay

The application includes a built-in tab-based help system loaded from local HTML files.

---

## Application Layout

The GUI is organised into three tabs:

### 1. Setup / Generate

Used to define the starting LUT parameters and generate an initial compensation profile.

### 2. Tuning

Used to edit points, adjust amplitude and phase, select interpolation styles, preview the rendered LUT, and apply live tones.

### 3. Save / Store

Used to save the LUT to file or store it to the connected synthesiser.

---

## Repository Contents

Typical key files in this repository include:

```text
Compensation_tool.py
build_local.bat
iCSA-iVSA_Compensation_Build_Tool.spec
requirements.txt
version_info.txt
ims_events.py
ims_scan.py
Isomet.ico
Splash.jpg
Changelog.md
LICENSE
```

### File summary

* `Compensation_tool.py` — main application
* `build_local.bat` — local Windows build script
* `iCSA-iVSA_Compensation_Build_Tool.spec` — PyInstaller build specification
* `requirements.txt` — Python package requirements
* `version_info.txt` — Windows executable version metadata
* `ims_events.py` — helper module derived from SDK examples
* `ims_scan.py` — helper module for device scanning
* `Isomet.ico` — application icon
* `Splash.jpg` — splash image used in packaged builds
* `Changelog.md` — release history
* `LICENSE` — project license

---

## Dependencies

Install the required packages with:

```bash
pip install -r requirements.txt
```

Main dependencies:

* `PySide6`
* `imslib`
* `matplotlib`
* `pyinstaller`

Current versions in `requirements.txt`:

```text
PySide6 == 6.10.2
matplotlib == 3.10.8
pyinstaller == 6.19.0
imslib == 2.0.8
```

---

## External Code References

This project includes code derived from example and helper utilities provided in the [Isomet imslib-python repository](https://github.com/Isomet-Corporation/imslib-python).

Included modules:

* `ims_events.py`
* `ims_scan.py`

These are used to support device scanning and event-driven SDK workflows.

---

## Precompiled Releases

The latest precompiled Windows executable is available from:

[**GitHub Releases**](https://github.com/Isomet-Corporation/iCSA-iVSA_Compensation_Build_Tool/releases)

Typical release file:

```text
iCSA-iVSA_Compensation_Build_Tool.exe
```

---

## Building the EXE Locally

### Option 1 — Build Script

Run the local build script:

```bash
build_local.bat
```

This script will:

* remove any existing local virtual environment
* create a new virtual environment
* install dependencies
* build the executable with PyInstaller

### Option 2 — Manual Build

```bash
pip install -r requirements.txt
pyinstaller iCSA-iVSA_Compensation_Build_Tool.spec
```

Typical output:

```text
dist/iCSA-iVSA Compensation Build Tool.exe
```

---

## Running the Application

### Run from Python

```bash
pip install -r requirements.txt
python Compensation_tool.py
```

### Run the compiled version

```bash
iCSA-iVSA_Compensation_Build_Tool.exe
```

Or run the built executable directly from the `dist` folder after a local build.

---

## Typical Workflow

A typical workflow is:

1. Launch the application
2. Connect to a compatible synthesiser
3. Generate a starting LUT from the setup tab
4. Tune amplitude and phase points in the tuning tab
5. Preview the rendered LUT response
6. Save the LUT to file, or
7. Download, verify, and store it as the synthesiser default

---

## Notes

* The application is intended for use with compatible IMS hardware.
* Most core functionality depends on a successful device connection.
* The project includes local PyInstaller support for building a Windows executable.
* The executable version information is supplied through `version_info.txt`.

---

