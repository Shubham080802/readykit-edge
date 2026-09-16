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

**If `winget` is not available, or the ARM64 MSI installer fails with `Error
0x80070003` / `BA aborted verify of payload`** (a Burn-bootstrapper payload
verification failure seen on some locked-down machines, unrelated to a
corrupted download - re-downloading and re-verifying the hash will not fix
it), skip the installer entirely and use the embeddable build instead:

```powershell
Invoke-WebRequest https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-arm64.zip -OutFile py.zip
Expand-Archive py.zip C:\Python312-arm64
Remove-Item C:\Python312-arm64\python312._pth      # see below - do not keep this
Invoke-WebRequest https://bootstrap.pypa.io/get-pip.py -OutFile get-pip.py
C:\Python312-arm64\python.exe get-pip.py
```

**Deleting `python312._pth` is the important step.** That file hardcodes
`sys.path`, and in doing so it disables three things everything downstream
needs: `PYTHONPATH` (so pip's build isolation silently cannot find its own
temp environment, and every source build dies with
`BackendUnavailable: Cannot import ...`), `site` processing (so `.pth` files
from editable installs are ignored), and the automatic
script-directory-on-`sys.path` rule (so any build script that imports a helper
module sitting beside it fails - OpenCV's `gen2.py` importing `hdr_parser` is
one). Removing it makes the embeddable build behave like a normal install:
it still finds its stdlib in `python312.zip` next to `python.exe`.

There is still no `venv` module, so there is no per-project virtual
environment - this *is* the environment. Install into it directly:

```powershell
C:\Python312-arm64\python.exe -m pip install -e ".[dev,console]"
```

Add `C:\Python312-arm64` and `C:\Python312-arm64\Scripts` to `PATH` so
`readykit`, `pytest`, etc. resolve directly instead of needing the full path
every time.

### If you need the dev headers too

The embeddable build ships no `include\` or `libs\`, so nothing can compile a
C extension against it - which matters if you end up building OpenCV
(stage 5). They are in the installer you could not run, and `msiexec /a`
extracts an MSI without installing it. Pull `dev.msi` out of the bundle's
attached container with 7-Zip, then:

```powershell
msiexec /a dev.msi /qn TARGETDIR=C:\pydev
Copy-Item C:\pydev\include C:\Python312-arm64\include -Recurse
Copy-Item C:\pydev\libs    C:\Python312-arm64\libs    -Recurse
```

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
`--engine geniex` and compare `--device` pinned to the NPU against CPU.

Read that comparison carefully, because it has a trap. It only means anything
for a model that *can* run on both. The Qualcomm AI Hub W4A16 bundles - the
default `qualcomm/Qwen3-VL-4B-Instruct` among them - are compiled for the
Hexagon NPU only, and a device pin they cannot honour is **ignored, not
refused**. Measured here: `qairt:npu` and `qairt:cpu` produced identical
latency, and GenieX reported `device=NPU, backend=qairt` for both, and for a
bare `cpu` pin as well. Identical numbers there mean the pin did nothing -
the opposite of the naive reading. Use `--require-npu`, which reads the device
back rather than inferring it from a stopwatch, and keep the latency
comparison for GGUF models, which genuinely do run either way.

Once GenieX is installed, add **`--require-npu`** to any command. It reads back
what the model actually loaded onto and refuses to run unless that is an NPU
backend:

```powershell
.venv\Scripts\readykit inspect --manifest manifests\trauma-kit-a.json `
  --engine geniex --camera 0 --require-npu
```

It refuses in two cases, not one: when the model demonstrably landed on the
CPU, and when the build will not say what it landed on. The second is
deliberate - `device_map="auto"` reads back as `"auto"` whether it fell back to
the CPU or not, so "I cannot tell" has to be a refusal rather than a shrug.
Without the flag the device is still recorded on every inspection, marked
`?unverified` when it could not be read.

Use it for anything you will quote a latency figure from. A silent CPU
fallback does not look like a failure; it looks like a slightly slow demo.

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
compile. If it fails with `BackendUnavailable: Cannot import '_geniex_backend'`,
build isolation could not reach the sdist's own in-tree backend - on the
embeddable build in stage 1, that is the `._pth` file, and deleting it as
described there fixes this too. Otherwise, pre-install the backend's build
dependencies and disable isolation for this one package:

```powershell
.venv\Scripts\pip install --no-build-isolation setuptools wheel tomli
.venv\Scripts\pip install --no-build-isolation geniex
.venv\Scripts\python -c "import geniex; print('ok')"
```

If it *still* fails here, that is a question for the Qualcomm engineers on
site, and a specific one: *the CLI installs and runs, but the Python binding
will not build.*

**If it cannot be fixed quickly, you are not stuck.** `--engine ollama` runs
the same prompt and the same parser against a real vision model on any
machine:

```powershell
winget install --id Ollama.Ollama -e
ollama pull qwen2.5vl:7b
.venv\Scripts\readykit inspect --manifest manifests\trauma-kit-a.json `
  --engine ollama --image tray.jpg
```

Same model family as the GenieX default, so only the runtime differs. It is
**not the NPU** and never claims to be: every record says `ollama:<model>`.
Say so out loud if you demo this way - but a working demonstration on the
wrong runtime beats a broken one on the right runtime.

---

## Stage 4 — The model

```powershell
geniex pull qualcomm/Qwen3-VL-4B-Instruct
geniex infer qualcomm/Qwen3-VL-4B-Instruct
```

`geniex list` is the source of truth for the repo id actually cached on this
machine - the `qualcomm/` namespace is what the CLI resolves to today, not
`ai-hub-models/`, despite what older GenieX docs may show. Check `geniex
list` before assuming a repo id is wrong.

Note how long the pull takes and how much disk it uses — you will want that
number when planning the demo.

### Why 4B and not something larger

Qualcomm AI Hub lists **Snapdragon X Elite** explicitly among the supported
chipsets for `Qwen3-VL-4B-Instruct`, which is the machine you are on. Two more
reasons it is the right default here:

- The sentinel inspects continuously rather than once. Roughly half the
  parameters is roughly half the time to first token, and a model taking three
  seconds a frame turns *"the latch closes under your hand"* into *"the latch
  closes eventually"*.
- Reading item names and printed dates off a tray is a legible-text task, not
  a reasoning-heavy one. A larger model's extra capacity is not spent on
  anything this asks of it.

`qualcomm/Qwen3-VL-8B-Instruct` and the Qwen2.5-VL family are the steps up
if a particular kit turns out to need one. Measure before you switch:

```powershell
.venv\Scripts\readykit bench --manifest manifests\trauma-kit-a.json `
  --engine geniex --model qualcomm/Qwen3-VL-4B-Instruct --require-npu --runs 20
```

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
camera capture.

Two ways to get the camera back: use ffmpeg instead (below - simpler, and the
default recommendation), or build OpenCV yourself (further down - about half
an hour, and then `--camera` works natively).

### Use `--ffmpeg-camera`, not `--camera`

ffmpeg ships a native ARM64 Windows build, so capture goes through it instead:

```powershell
winget install --id Gyan.FFmpeg -e
```

Open a new terminal, then find the camera's DirectShow name - dshow wants the
name, not an index:

```powershell
ffmpeg -hide_banner -f dshow -list_devices true -i dummy
.venv\Scripts\readykit doctor --cameras
```

Then pass it:

```powershell
.venv\Scripts\readykit inspect --manifest manifests\trauma-kit-a.json `
  --engine geniex --require-npu --ffmpeg-camera "HD Webcam C270"
```

It yields encoded JPEG bytes, which is what GenieX wants anyway - it takes
image *file paths*, so a pixel array would only have to be re-encoded.

Costs about 600ms per frame, almost all of it spawning the process and opening
the device, so the warmup frames are close to free. That is well under what a
7B VLM takes per frame, so it is not the bottleneck - but keep `sentinel
--interval` above a second when using it.

**If it will not open the camera**, two fallbacks, in order:

1. **Capture to a file with any tool** and use `--image <path>`. Same pipeline,
   same records, no camera driver involved.
2. **Run the camera stage in simulation** while using the real model and the
   real board. Say so out loud if you do.

Do not solve this by installing x64 Python under emulation. It would get OpenCV
working and break GenieX, which is the wrong trade.

### Or build OpenCV for ARM64 yourself

There is no upstream wheel, but the source builds fine natively. Confirmed
working: OpenCV 5.0.0, `cv2.cp312-win_arm64.pyd`, `--camera` and
`cv2.imencode` both live. Budget about half an hour of compile time.

You need three things PyPI will not give you: an ARM64-hosted C++ toolchain
([llvm-mingw](https://github.com/mstorsjo/llvm-mingw/releases), the
`ucrt-aarch64` build), [CMake](https://cmake.org/download/) and
[Ninja](https://github.com/ninja-build/ninja/releases) (both ship
`windows-arm64` zips), and the Python dev headers from the section in stage 1.

```powershell
cmake -G Ninja `
  -DCMAKE_C_COMPILER=aarch64-w64-mingw32-gcc.exe `
  -DCMAKE_CXX_COMPILER=aarch64-w64-mingw32-g++.exe `
  -DCMAKE_BUILD_TYPE=Release `
  -DBUILD_LIST="core,imgproc,imgcodecs,videoio,python3" `
  -DPYTHON3_EXECUTABLE="C:/Python312-arm64/python.exe" `
  -DPYTHON3_INCLUDE_DIR="C:/Python312-arm64/include" `
  -DPYTHON3_LIBRARY="C:/Python312-arm64/libs/python312.lib" `
  -DPYTHON3_NUMPY_INCLUDE_DIRS="C:/Python312-arm64/Lib/site-packages/numpy/_core/include" `
  -DBUILD_TESTS=OFF -DBUILD_PERF_TESTS=OFF -DBUILD_EXAMPLES=OFF -DBUILD_DOCS=OFF `
  C:/path/to/opencv-5.0.0
ninja && ninja install
```

Four things will bite, in the order you will hit them:

- **Use forward slashes in every `-D` path.** CMake parses `\U` in
  `C:\Users\...` as an invalid escape and dies during configure.
- **`BUILD_LIST` is doing real work.** The bundled MLAS (in the `dnn` module)
  only compiles ARM64 NEON intrinsics under MSVC - against clang it fails with
  `unknown type name 'float32x4_t'`. `stereo` fails separately on
  `std::back_inserter` without `<iterator>`. Restricting the list sidesteps
  both, and readykit only needs `imgcodecs` and `videoio` anyway.
- **`modules/python/src2/cv2.cpp` needs `#include <iterator>` added.** Same
  `std::back_inserter` problem: llvm-mingw uses libc++, which does not leak
  that header transitively the way libstdc++ and MSVC do.
- **The `.pyd` needs its DLLs beside it.** Copy the six `libopencv_*500.dll`
  from the build's `bin\`, plus `libc++.dll`, `libunwind.dll` and
  `libwinpthread-1.dll` from the toolchain, into
  `site-packages\cv2\python-3.12\`. Without them the import fails with
  `DLL load failed while importing cv2`.

`ninja install` does not write package metadata, so `readykit doctor` will
report `unknown version` and pip will not list it. Hand-write a
`opencv_python-5.0.0.dist-info` with `METADATA`, `WHEEL`, `INSTALLER`,
`top_level.txt` and a `RECORD` to fix both.

**Back the result up once it works.** It is half an hour of compiling and
five separate workarounds, and it is about 10 MB zipped:

```powershell
Compress-Archive -Path C:\Python312-arm64\Lib\site-packages\cv2, `
                       C:\Python312-arm64\Lib\site-packages\opencv_python-5.0.0.dist-info `
                 -DestinationPath cv2-win-arm64-py312.zip
```

Restoring is unzipping it back into `site-packages`. Keep a `pip freeze`
beside it - this environment is hand-built rather than reproducible from
`pyproject.toml` alone, and the day you need it is the day it broke.

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

Flash `firmware\mcu_actuator\mcu_actuator.ino` to the **STM32U585 core**.
`arduino-cli` is simpler than the Arduino IDE here because the board's real
FQBN is `arduino:zephyr:unoq` - **not** `arduino:stm32:unoq`, which does not
exist for this board - and that core is only in Arduino's staging package
index today:

```powershell
arduino-cli config init
arduino-cli core update-index --additional-urls https://downloads.arduino.cc/packages/package_staging_index.json
arduino-cli core install arduino:zephyr --additional-urls https://downloads.arduino.cc/packages/package_staging_index.json
arduino-cli lib install Arduino_RouterBridge
arduino-cli board list
```

`board list` should show the board on a `COMn` port with FQBN
`arduino:zephyr:unoq`. If compiling fails with `Please install the
Arduino_RouterBridge library`, the `lib install` step above was skipped or
failed silently - this board's `Serial` is implemented over an RPC bridge to
the Linux side, not a raw UART, and that library is what provides it. Then:

```powershell
arduino-cli compile --upload -p COMn -b arduino:zephyr:unoq firmware\mcu_actuator
```

This drives the board over USB via `adb` and SWD under the hood (bundled with
the core as the `remoteocd` tool) - no separate ST-Link or debug probe needed.
A verify failure on the very first flash of a fresh board is a known flake of
the bit-banged SWD link; retrying the same command is usually enough. Then
work through the [bring-up checklist](deployment.md#bring-up-checklist).

---

## The whole thing, end to end

```powershell
.venv\Scripts\readykit inspect --manifest manifests\trauma-kit-a.json `
  --engine geniex --model qualcomm/Qwen3-VL-4B-Instruct --device auto `
  --camera 0 --link serial --port COM4
```

---

## If you only remember one thing

Run **`.venv\Scripts\readykit doctor`** after every stage. It reports what is
installed, which port the board is on, whether the manifests parse and whether
the audit log is writable — each with the command that fixes it. It is faster
than reasoning about what broke.
