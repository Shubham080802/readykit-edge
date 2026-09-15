"""Camera capture that works where OpenCV will not install.

`opencv-python` has no `win_arm64` wheel and the Snapdragon X Elite is
`win_arm64`, so on the machine this project targets, `CameraSource` is not an
option at all. This is the path that has to work on the day.

Nothing here needs a camera or ffmpeg: the parts worth testing are the frame
splitting, the header parsing and the platform argument mapping - all pure,
and all things that fail in ways that look like something else.
"""

from __future__ import annotations

import sys

import pytest

from readykit.capture import (
    CaptureError,
    FfmpegCameraSource,
    _ffmpeg_input_args,
    _jpeg_size,
    _last_jpeg,
)

SOI = b"\xff\xd8\xff"
EOI = b"\xff\xd9"


def jpeg(body: bytes) -> bytes:
    return SOI + body + EOI


class TestPickingTheFrameOutOfTheStream:
    """MJPEG arrives as concatenated JPEGs; the last one is the exposed one."""

    def test_a_single_frame_comes_back_whole(self) -> None:
        frame = jpeg(b"only")
        assert _last_jpeg(frame) == frame

    def test_the_last_of_several_is_returned(self) -> None:
        """The earlier ones are warmup - underexposed while auto-gain settles.
        Returning the first would hand the model the worst available frame."""
        stream = jpeg(b"warm1") + jpeg(b"warm2") + jpeg(b"exposed")
        assert _last_jpeg(stream) == jpeg(b"exposed")

    def test_a_truncated_final_frame_is_refused(self) -> None:
        """ffmpeg killed mid-write leaves a partial JPEG. Passing that to a
        model is a guaranteed INDETERMINATE dressed up as a real reading."""
        assert _last_jpeg(jpeg(b"good") + SOI + b"cut off") is None

    def test_no_image_at_all_is_refused(self) -> None:
        assert _last_jpeg(b"ffmpeg: some error text") is None

    def test_empty_output_is_refused(self) -> None:
        assert _last_jpeg(b"") is None


class TestReadingTheSize:
    def test_dimensions_come_off_the_frame_header(self) -> None:
        # SOF0: marker, length, precision, height, width, components
        sof = bytes([0xFF, 0xC0, 0x00, 0x11, 0x08, 0x02, 0x40, 0x01, 0x90, 0x03])
        assert _jpeg_size(b"\xff\xd8" + sof + b"\xff\xd9") == (400, 576)

    def test_a_missing_header_reports_zero_rather_than_guessing(self) -> None:
        assert _jpeg_size(b"\xff\xd8\xff\xd9") == (0, 0)

    def test_garbage_does_not_hang(self) -> None:
        assert _jpeg_size(b"\xff" * 200) == (0, 0)

    def test_a_zero_length_segment_does_not_loop_forever(self) -> None:
        """A malformed segment length of 0 would advance the cursor by 2 each
        pass and never terminate if it were not guarded."""
        assert _jpeg_size(b"\xff\xd8\xff\xe0\x00\x00" + b"\x00" * 40) == (0, 0)


class TestNamingTheCameraPerPlatform:
    """Each platform spells this differently, and a wrong flag produces an
    ffmpeg error that reads like a missing file."""

    def test_macos_uses_avfoundation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "darwin")
        args = _ffmpeg_input_args("0", 30)
        assert "avfoundation" in args
        assert args[-1] == "0"

    def test_windows_uses_dshow_with_a_device_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        args = _ffmpeg_input_args("HD Webcam", 30)
        assert "dshow" in args
        assert args[-1] == "video=HD Webcam"

    def test_windows_does_not_double_prefix(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        assert _ffmpeg_input_args("video=Cam", 30)[-1] == "video=Cam"

    def test_linux_maps_an_index_to_a_device_node(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        assert _ffmpeg_input_args("2", 30)[-1] == "/dev/video2"

    def test_linux_accepts_an_explicit_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        assert _ffmpeg_input_args("/dev/video9", 30)[-1] == "/dev/video9"


class TestConstruction:
    def test_missing_ffmpeg_is_reported_at_construction(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Not on the first frame, and with the install command in the text."""
        monkeypatch.setattr("readykit.capture.shutil.which", lambda _: None)
        with pytest.raises(CaptureError, match="ffmpeg is not on PATH"):
            FfmpegCameraSource()

    def test_negative_warmup_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("readykit.capture.shutil.which", lambda _: "/usr/bin/ffmpeg")
        with pytest.raises(CaptureError, match="warmup_frames"):
            FfmpegCameraSource(warmup_frames=-1)

    def test_warmup_means_one_extra_capture_each(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("readykit.capture.shutil.which", lambda _: "/usr/bin/ffmpeg")
        assert FfmpegCameraSource(warmup_frames=4)._frames == 5
        assert FfmpegCameraSource(warmup_frames=0)._frames == 1
