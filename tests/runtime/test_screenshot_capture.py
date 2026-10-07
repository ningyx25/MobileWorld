"""Tests for screenshot capture integrity handling.

``adb exec-out screencap -p > file`` can exit 0 while writing a truncated PNG
(a strict prefix of a valid file, so the IHDR header parses but the pixel
decode fails). Such a file used to be served to clients as a valid frame and
crashed their image pipeline. The capture now validates the file and falls
back to the shell+pull path, retrying within ``try_times``.
"""

import io

from PIL import Image

from mobile_world.runtime import controller as controller_module
from mobile_world.runtime.controller import AndroidController
from mobile_world.runtime.utils.helpers import (
    AdbResponse,
    is_complete_png_file,
    verify_png_file,
)


def _png_bytes(width=20, height=20):
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color=(5, 6, 7)).save(buf, format="PNG")
    return buf.getvalue()


def _truncated_png_bytes():
    data = _png_bytes()
    return data[: len(data) // 2]


def _sequence(values):
    """Return a callable serving ``values`` in order (the last one repeats)."""
    queue = list(values)

    def _next():
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return _next


class _FakeAdb:
    """Simulates the adb commands ``get_screenshot`` issues.

    ``exec_out`` / ``pull`` are callables returning the bytes that capture
    path should write, or ``None`` to fail the command.
    """

    def __init__(self, exec_out=None, pull=None):
        self.exec_out = exec_out or _sequence([None])
        self.pull = pull or _sequence([None])
        self.commands = []
        self.exec_out_calls = 0
        self.pull_calls = 0

    def __call__(self, command, output=True, root_required=False):
        self.commands.append(command)
        if "shell wm size" in command:
            return AdbResponse(success=True, output="Physical size: 1080x2400")
        if "exec-out screencap" in command:
            self.exec_out_calls += 1
            data = self.exec_out()
            if data is None:
                return AdbResponse(success=False, error="exec-out failed")
            with open(command.split("> ", 1)[1].strip(), "wb") as f:
                f.write(data)
            return AdbResponse(success=True, output="")
        if "shell screencap -p " in command:
            return AdbResponse(success=True, output="")
        if " pull " in command:
            self.pull_calls += 1
            tokens = command.split()
            local = tokens[-1]
            data = self.pull()
            if data is None:
                return AdbResponse(success=False, error="pull failed")
            with open(local, "wb") as f:
                f.write(data)
            return AdbResponse(success=True, output="")
        return AdbResponse(success=True, output="")


def _make_controller(monkeypatch, tmp_path, fake_adb):
    monkeypatch.setattr(controller_module, "execute_adb", fake_adb)
    monkeypatch.setattr(controller_module.time, "sleep", lambda *_: None)
    return AndroidController(device="emulator-5554")


# ---------------------------------------------------------------------------
# File validators
# ---------------------------------------------------------------------------


def test_is_complete_png_file_accepts_valid_png(tmp_path):
    path = tmp_path / "shot.png"
    path.write_bytes(_png_bytes())
    assert is_complete_png_file(str(path))


def test_is_complete_png_file_rejects_truncated_empty_and_missing(tmp_path):
    truncated = tmp_path / "truncated.png"
    truncated.write_bytes(_truncated_png_bytes())
    empty = tmp_path / "empty.png"
    empty.write_bytes(b"")

    assert not is_complete_png_file(str(truncated))
    assert not is_complete_png_file(str(empty))
    assert not is_complete_png_file(str(tmp_path / "missing.png"))


def test_verify_png_file_rejects_truncated(tmp_path):
    good = tmp_path / "good.png"
    good.write_bytes(_png_bytes())
    truncated = tmp_path / "truncated.png"
    truncated.write_bytes(_truncated_png_bytes())

    assert verify_png_file(str(good))
    assert not verify_png_file(str(truncated))


# ---------------------------------------------------------------------------
# get_screenshot fallback / retry
# ---------------------------------------------------------------------------


def test_get_screenshot_falls_back_when_exec_out_truncates(monkeypatch, tmp_path):
    fake = _FakeAdb(
        exec_out=_sequence([_truncated_png_bytes()]),
        pull=_sequence([_png_bytes()]),
    )
    controller = _make_controller(monkeypatch, tmp_path, fake)

    result = controller.get_screenshot("shot", str(tmp_path))

    assert result.success is True
    assert result.output == str(tmp_path / "shot.png")
    # exec-out ran once (truncated) and the pull fallback recovered the frame.
    assert fake.exec_out_calls == 1
    assert fake.pull_calls == 1
    assert is_complete_png_file(result.output)


def test_get_screenshot_fails_when_every_path_truncates(monkeypatch, tmp_path):
    fake = _FakeAdb(
        exec_out=_sequence([_truncated_png_bytes()]),
        pull=_sequence([_truncated_png_bytes()]),
    )
    controller = _make_controller(monkeypatch, tmp_path, fake)

    result = controller.get_screenshot("shot", str(tmp_path), try_times=2)

    assert result.success is False
    assert "invalid PNG" in result.error
    # try_times=2 means three capture rounds, same budget as before.
    assert fake.exec_out_calls == 3


def test_get_screenshot_retries_a_failed_pull(monkeypatch, tmp_path):
    fake = _FakeAdb(
        exec_out=_sequence([None]),  # stealth path always fails
        pull=_sequence([None, _png_bytes()]),  # pull fails once, then succeeds
    )
    controller = _make_controller(monkeypatch, tmp_path, fake)

    result = controller.get_screenshot("shot", str(tmp_path), try_times=2)

    assert result.success is True
    assert fake.pull_calls == 2
    assert is_complete_png_file(result.output)
