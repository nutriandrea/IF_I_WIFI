#!/usr/bin/env python3
"""
Tests for csi_plot.py and csi_record.py — verifica parsing, replay, info.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

# ── helpers ──────────────────────────────────────────────

def make_csi_line(seq: int, rssi: int = -50, noise: int = -90,
                  rate: int = 6, bw: int = 20, sub_count: int = 16) -> str:
    """Generate a synthetic CSI line (realistic amplitudes)."""
    import random
    data = []
    for s in range(sub_count):
        base = 30 - abs(s - sub_count // 2) * 0.3
        ampl = base + random.gauss(0, 2)
        real = int(ampl * random.uniform(0.4, 0.9))
        imag = int(ampl * random.uniform(0.1, 0.6))
        data.append(str(real))
        data.append(str(imag))
    csv = ",".join(data)
    return f"CSI:{seq}:{rssi}:{noise}:{rate}:{bw}:{sub_count}:{csv}"


def make_multi_ap_lines(seq_start: int, count: int, ap_id: int) -> list[str]:
    lines = []
    lines.append(f"#AP {ap_id}")
    lines.append(f"#SWITCH {ap_id}")
    for i in range(count):
        lines.append(make_csi_line(seq_start + i, rssi=-50 + ap_id * 5))
    return lines


# ── tests ────────────────────────────────────────────────

def test_record_info_roundtrip():
    """Record → info should parse correctly."""
    from csi.csi_record import cmd_info
    import argparse

    import random
    random.seed(42)

    lines = make_multi_ap_lines(1, 50, 0)

    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt",
                                     delete=False, prefix="csi_test_") as f:
        f.write("\n".join(lines) + "\n")
        fpath = f.name

    try:
        # cmd_info returns 0 on success
        ret = cmd_info(argparse.Namespace(info=fpath))
        assert ret == 0, f"cmd_info returned {ret}"
    finally:
        os.unlink(fpath)

    print("  ✅ test_record_info_roundtrip")


def test_info_statistics():
    """Verify info reports correct frame count and RSSI stats."""
    from csi.csi_record import cmd_info
    import argparse

    import random
    random.seed(1)

    lines = [make_csi_line(i, rssi=-60 + i % 10) for i in range(1, 101)]

    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt",
                                     delete=False, prefix="csi_test_") as f:
        f.write("\n".join(lines) + "\n")
        fpath = f.name

    try:
        ret = cmd_info(argparse.Namespace(info=fpath))
        assert ret == 0
    finally:
        os.unlink(fpath)

    print("  ✅ test_info_statistics")


def test_replay_stdout():
    """Replay to stdout should produce exactly the same lines."""
    from csi.csi_record import cmd_replay_stdout
    import argparse

    import random
    random.seed(7)

    original = [make_csi_line(i) for i in range(1, 11)]

    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt",
                                     delete=False, prefix="csi_test_") as f:
        f.write("\n".join(original) + "\n")
        fpath = f.name

    try:
        from io import StringIO
        old_stdout = sys.stdout
        sys.stdout = StringIO()
        try:
            ret = cmd_replay_stdout(argparse.Namespace(
                replay=fpath, rate=None))
            assert ret == 0
        finally:
            output = sys.stdout.getvalue()
            sys.stdout = old_stdout

        replayed = [l for l in output.split("\n") if l.startswith("CSI:")]
        assert len(replayed) == len(original), \
            f"Expected {len(original)} lines, got {len(replayed)}"
    finally:
        os.unlink(fpath)

    print("  ✅ test_replay_stdout")


def test_replay_rate_limited():
    """Replay with rate limit should not crash."""
    from csi.csi_record import cmd_replay_stdout
    import argparse

    import random
    random.seed(13)

    original = [make_csi_line(i) for i in range(1, 21)]

    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt",
                                     delete=False, prefix="csi_test_") as f:
        f.write("\n".join(original) + "\n")
        fpath = f.name

    try:
        ret = cmd_replay_stdout(argparse.Namespace(
            replay=fpath, rate=100))
        assert ret == 0
    finally:
        os.unlink(fpath)

    print("  ✅ test_replay_rate_limited")


def test_replay_parse_multi_ap():
    """Replay with AP markers should not cause errors."""
    from csi.csi_record import cmd_replay_stdout
    import argparse

    import random
    random.seed(99)

    lines = make_multi_ap_lines(1, 30, 0) + \
            make_multi_ap_lines(31, 30, 1)

    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt",
                                     delete=False, prefix="csi_test_") as f:
        f.write("\n".join(lines) + "\n")
        fpath = f.name

    try:
        ret = cmd_replay_stdout(argparse.Namespace(
            replay=fpath, rate=None))
        assert ret == 0
    finally:
        os.unlink(fpath)

    print("  ✅ test_replay_parse_multi_ap")


def test_plot_class_instantiation():
    """Plot viewer classes should instantiate without error."""
    from csi.csi_plot import WaterfallPlot, TimePlot, BarPlot
    import matplotlib
    matplotlib.use("Agg")

    import matplotlib.pyplot as plt

    fig, ax = plt.subplots()

    wf = WaterfallPlot(ax, 64)
    assert wf.n_sub == 64
    assert wf.image is not None

    ax.cla()
    tp = TimePlot(ax, [0, 16, 32], window=10)
    assert 0 in tp.data
    assert 16 in tp.data

    ax.cla()
    bp = BarPlot(ax)
    assert bp.n_sub > 0

    plt.close("all")
    print("  ✅ test_plot_class_instantiation")


def test_plot_update_bar():
    """BarPlot.update() should handle parsed CSI data."""
    from csi.csi_plot import BarPlot
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots()
    bp = BarPlot(ax)

    parsed = {
        "seq": 1, "rssi": -45, "csi": [
            {"subcarrier": i, "real": 10.0, "imag": 0.0,
             "ampl": 10.0, "phase": 0.0}
            for i in range(8)
        ],
        "ampl_mean": 10.0, "ampl_std": 0.0,
        "num_subcarriers": 8,
    }
    bp.update(parsed)
    assert bp.bars is not None
    assert len(bp.bars) == 8

    plt.close("all")
    print("  ✅ test_plot_update_bar")


def test_plot_update_waterfall():
    """WaterfallPlot.update() should handle parsed CSI data."""
    from csi.csi_plot import WaterfallPlot
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots()
    wf = WaterfallPlot(ax, 64)

    parsed = {
        "seq": 1, "rssi": -45, "rate": 6,
        "csi": [{"subcarrier": i, "ampl": float(5 + i % 10)}
                for i in range(16)],
    }
    wf.update(parsed)
    assert len(wf.buffer) == 1

    plt.close("all")
    print("  ✅ test_plot_update_waterfall")


# ── main ─────────────────────────────────────────────────

def main():
    tests = [
        test_record_info_roundtrip,
        test_info_statistics,
        test_replay_stdout,
        test_replay_rate_limited,
        test_replay_parse_multi_ap,
        test_plot_class_instantiation,
        test_plot_update_bar,
        test_plot_update_waterfall,
    ]

    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except Exception as e:
            print(f"  ❌ {t.__name__}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    total = passed + failed
    print(f"\n{'='*50}")
    print(f"  {total} tests: {passed} passed, {failed} failed")
    print(f"{'='*50}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
