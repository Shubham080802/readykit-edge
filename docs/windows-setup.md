# Setting up a Snapdragon X Elite laptop

For a freshly formatted Windows ARM64 machine (Qualcomm Oryon CPU) with nothing
on it.

The order matters. Each stage ends with something you can check, so a failure
tells you which stage broke rather than leaving you guessing at the end.

> **Everything here is PowerShell.** Paths use `.venv\Scripts\`, not
> `.venv/bin/`. `readykit doctor` prints whichever form applies on the machine
> it is running on.

---

## Stage 1 — Python and Git

```powershell
winget install --id Python.Python.3.12 -e
winget install --id Git.Git -e
```

Close the terminal and open a new one, so `PATH` picks them up.

**Check — and this one matters:**

```powershell
python -c "import platform, sys; print(platform.machine(), sys.version)"
```

It must say **`ARM64`**. If it says `AMD64`, you have the x64 build running
under emulation. Uninstall it and install the ARM64 one, because an emulated
interpreter cannot load the native GenieX runtime later — and you will not find
that out until stage 4.

---

## Stage 2 — The project, in simulation

This stage needs no camera, no board and no model. Getting it green proves the
laptop, Python and the toolchain all work, before anything harder is involved.

```powershell
git clone https://github.com/taranggoyal70/readykit-edge.git
cd readykit-edge

python -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\pip install -e ".[dev,console]"
```

**Check:**

```powershell
.venv\Scripts\python -m pytest
.venv\Scripts\readykit inspect --manifest manifests\trauma-kit-a.json --scene complete --no-log
.venv\Scripts\readykit compare --manifest manifests\trauma-kit-a.json
```

The tests should pass, the inspection should print `PASS`, and `compare` should
print the table. If this works, the machine is fine and everything after this
is about hardware.

The `[host]` extra is deliberately *not* installed yet — see stage 5.

---

## Is there actually an NPU?

Every Snapdragon X Elite has a Hexagon NPU on the die, so the question is never
whether one is fitted — it is whether the driver is installed and Windows is
exposing it. A machine with the silicon and no driver looks exactly like a
machine without one, right up until inference quietly lands on the CPU and the
latency figures stop meaning anything.

```powershell
.venv\Scripts\readykit doctor
```

Or directly:

```powershell
Get-PnpDevice -PresentOnly |
  Where-Object { $_.FriendlyName -match 'NPU|Neural|Hexagon|AI Boost' } |
  Format-Table Status, Class, FriendlyName
```

You are looking for a device that is **present and `OK`**. Anything else will
not be used. Two other places to look:

- **Task Manager → Performance** — Windows 11 lists NPU beside CPU and GPU
- **Device Manager** — under *Neural processors*

If nothing appears, that is a driver question for the Qualcomm engineers.
Inference still works without it; it falls back to CPU and gets slow, which is
survivable for a demo but not something to discover on stage.

**The check that actually settles it** comes later: run `readykit bench` with
`--engine geniex` and compare `--device` pinned to the NPU against CPU. If the
numbers are the same, you are not on the NPU whatever Device Manager says.

## Stage 3 — GenieX

Two separate installs, and **the order is not optional**: the Python package is
a binding, and it wants the native runtime already present.

**3a. The runtime.** Download `geniex-cli-setup-windows-arm64-<version>.exe`
from [the releases page](https://github.com/qualcomm/GenieX/releases), run it,
then **open a new terminal** so `PATH` updates.

```powershell
geniex --version
```

**3b. The Python binding.**

```powershell
.venv\Scripts\pip install geniex
.venv\Scripts\python -c "import geniex; print('ok')"
```

PyPI ships `geniex` as a source archive rather than a wheel, so this step may
compile. If it fails here, that is a question for the Qualcomm engineers on
site, and a specific one: *the CLI installs and runs, but the Python binding
will not build.*

---

## Stage 4 — The model

```powershell
geniex pull ai-hub-models/Qwen2.5-VL-7B-Instruct
geniex infer ai-hub-models/Qwen2.5-VL-7B-Instruct
```

Note how long the pull takes and how much disk it uses — you will want that
number when planning the demo.

The `infer` step is the real test. Give it an image and ask what is in it. **If
this does not work, nothing downstream will**, and everything after this point
is wasted effort until it does.

It must be a **vision** model. `GenieXEngine` refuses a text-only one at
startup rather than let it answer confidently about an image it never saw.

---

## Stage 5 — The camera, and the one thing that does not just work

`opencv-python` has **no Windows ARM64 wheel** — only `win32` and `win_amd64`.
So this will fail:

```powershell
.venv\Scripts\pip install -e ".[host]"     # opencv has no ARM64 wheel
```

`pyserial` and `numpy` are fine, so install those directly and leave OpenCV out:

```powershell
.venv\Scripts\pip install pyserial numpy pillow
```

That gets you the **serial link to the Arduino** and everything except live
camera capture. Options for capture, in the order worth trying:

1. **Ask the Qualcomm engineers.** They will know whether there is an ARM64
   OpenCV build for these machines. It is exactly the kind of thing they are
   there for, and it costs you one question.
2. **Capture to a file with any tool** and point `ImageFileSource` at it. The
   pipeline already supports this, and GenieX takes image *file paths* anyway.
3. **Run the demo in simulation** for the camera stage while using the real
   model and the real board. Say so out loud if you do.

Do not solve this by installing x64 Python under emulation. It would get OpenCV
working and break GenieX, which is the wrong trade.

---

## Stage 6 — The board

When the Arduino UNO Q arrives:

```powershell
.venv\Scripts\readykit doctor
```

It enumerates the serial ports and flags the one that looks like the board —
on Windows it appears as `COMn`. Then:

```powershell
.venv\Scripts\readykit inspect --manifest manifests\trauma-kit-a.json `
  --scene complete --link serial --port COM4
```

Flash `firmware\mcu_actuator\mcu_actuator.ino` to the **STM32U585 core** with
the Arduino IDE, and work through the
[bring-up checklist](deployment.md#bring-up-checklist).

---

## The whole thing, end to end

```powershell
.venv\Scripts\readykit inspect --manifest manifests\trauma-kit-a.json `
  --engine geniex --model ai-hub-models/Qwen2.5-VL-7B-Instruct --device auto `
  --camera 0 --link serial --port COM4
```

---

## If you only remember one thing

Run **`.venv\Scripts\readykit doctor`** after every stage. It reports what is
installed, which port the board is on, whether the manifests parse and whether
the audit log is writable — each with the command that fixes it. It is faster
than reasoning about what broke.
