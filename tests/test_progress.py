import threading
import time

import pytest

import landsat_record.progress as progress
from landsat_record.progress import PlainProgress, pool_map, progress_bar


def test_plain_lines_when_output_is_not_a_terminal(capsys, monkeypatch):
    monkeypatch.setattr(progress, "PLAIN_INTERVAL", 0)
    bar = progress_bar(4, "Crop", "scenes")  # pytest's captured output isn't a terminal
    assert isinstance(bar, PlainProgress)
    with bar:
        bar.update(1)
        bar.set_postfix_str("1 with errors")
        bar.update(1)
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("Crop: 1/4 scenes") and "left" in lines[0]
    assert lines[-1].startswith("Crop: 2/4 scenes") and lines[-1].endswith("1 with errors")


def test_pool_map_keeps_order(capsys):
    def slow_square(n):
        time.sleep(0.01 * (5 - n))
        return n * n

    seen = []
    with PlainProgress(5, "Test", "items") as bar:
        assert pool_map(slow_square, range(5), 5, bar, lambda item, result: seen.append(item)) == [0, 1, 4, 9, 16]
    assert sorted(seen) == [0, 1, 2, 3, 4]


def test_ctrl_c_cancels_what_has_not_started(capsys):
    started = []
    lock = threading.Lock()

    def work(n):
        with lock:
            started.append(n)
        if n == 0:
            raise KeyboardInterrupt
        time.sleep(0.05)
        return n

    with PlainProgress(100, "Test", "items") as bar, pytest.raises(KeyboardInterrupt):
        pool_map(work, range(100), 2, bar)
    assert len(started) < 100
    assert "Re-run the same command" in capsys.readouterr().out
