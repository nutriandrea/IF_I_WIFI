#!/usr/bin/env python3
"""
Test per room_mapper.py: FingerprintMap e PositionEstimator.

Esegue:
    python3 -m pytest test_room_mapper.py -v
"""

from __future__ import annotations

import json
import os
import tempfile
import time

import pytest

from mapping.room_mapper import FingerprintMap, PositionEstimator, FingerprintPoint


# ============================================================
# FingerprintMap
# ============================================================

class TestFingerprintMap:
    def test_create_empty(self):
        """Mappa vuota con dimensioni."""
        f = FingerprintMap(width=8, height=6, name="test")
        assert f.room["width"] == 8
        assert f.room["height"] == 6
        assert f.room["name"] == "test"
        assert len(f.points) == 0
        assert f.n_points == 0

    def test_add_point(self):
        """Aggiungere un punto funziona."""
        f = FingerprintMap(width=8, height=6)
        f.add_point_xy(2.0, 3.0, rssi=[-45, -50, -52], label="scrivania")
        assert f.n_points == 1
        p = f.points[0]
        assert p.x == 2.0
        assert p.y == 3.0
        assert p.rssi == [-45, -50, -52]
        assert p.label == "scrivania"
        assert p.timestamp > 0

    def test_add_point_no_label(self):
        """Punto senza label."""
        f = FingerprintMap(width=8, height=6)
        f.add_point_xy(1, 1, rssi=[-40, -41, -42])
        assert f.points[0].label == ""

    def test_save_and_load(self):
        """Salva e carica da file ripristina i punti."""
        f = FingerprintMap(width=8, height=6, name="salva_carica")
        f.add_point_xy(2, 3, rssi=[-45, -50, -52], label="scrivania")
        f.add_point_xy(5, 2, rssi=[-60, -58, -55], label="letto")

        tmp_path = tempfile.mktemp(suffix=".json")
        try:
            f.save(tmp_path)

            f2 = FingerprintMap.load(tmp_path)
            assert f2.room["width"] == 8
            assert f2.room["height"] == 6
            assert f2.room["name"] == "salva_carica"
            assert f2.n_points == 2
            assert f2.points[0].rssi == [-45, -50, -52]
            assert f2.points[1].label == "letto"
        finally:
            os.unlink(tmp_path)

    def test_info_string(self):
        """info() restituisce descrizione leggibile."""
        f = FingerprintMap(width=8, height=6)
        f.add_point_xy(1, 1, rssi=[-40, -41, -42])
        info = f.info()
        assert "8" in info
        assert "6" in info
        assert "1" in info

    def test_save_roundtrip(self):
        """save/load roundtrip preserva tutti i dati."""
        f = FingerprintMap(width=5, height=5, name="test")
        f.add_point_xy(2, 2, rssi=[-50, -51, -52], label="centro")
        tmp_path = tempfile.mktemp(suffix=".json")
        try:
            f.save(tmp_path)
            f2 = FingerprintMap.load(tmp_path)
            assert f2.room["width"] == 5
            assert f2.room["height"] == 5
            assert f2.room["name"] == "test"
            assert f2.num_aps == 3
            assert len(f2.points) == 1
            assert f2.points[0].label == "centro"
            assert f2.points[0].rssi == [-50, -51, -52]
        finally:
            os.unlink(tmp_path)

    def test_unique_timestamps(self):
        """Timestamp diversi per punti diversi."""
        f = FingerprintMap(width=5, height=5)
        f.add_point_xy(0, 0, rssi=[-40, -41, -42])
        time.sleep(0.01)
        f.add_point_xy(1, 1, rssi=[-43, -44, -45])
        assert f.points[1].timestamp > f.points[0].timestamp


# ============================================================
# PositionEstimator
# ============================================================

class TestPositionEstimator:
    def make_fmap(self) -> FingerprintMap:
        f = FingerprintMap(width=6, height=5)
        # 3 punti d'angolo
        f.add_point_xy(0, 0, rssi=[-30, -50, -70], label="angolo_0")
        f.add_point_xy(6, 0, rssi=[-70, -30, -50], label="angolo_1")
        f.add_point_xy(3, 5, rssi=[-50, -70, -30], label="angolo_2")
        return f

    def test_load_and_estimate(self):
        """Stima posizione da vettore RSSI."""
        f = self.make_fmap()
        est = PositionEstimator(k=3)
        est._fmap = f
        result = est.estimate([-35, -45, -65])
        assert "error" not in result, f"Error: {result.get('error')}"
        # Dovrebbe indovinare vicino a (0, 0)
        assert "x" in result and "y" in result
        assert result["confidence"] > 0

    def test_estimate_with_k1(self):
        """k=1 restituisce esattamente il punto piu' vicino."""
        f = self.make_fmap()
        est = PositionEstimator(k=1)
        est._fmap = f
        result = est.estimate([-30, -50, -70])
        assert result["x"] == 0
        assert result["y"] == 0
        assert result["confidence"] > 0

    def test_estimate_interpolation(self):
        """RSSI intermedio produce posizione interpolata."""
        f = FingerprintMap(width=4, height=4)
        f.add_point_xy(0, 2, rssi=[-40, -40, -40], label="sinistra")
        f.add_point_xy(4, 2, rssi=[-80, -80, -80], label="destra")
        est = PositionEstimator(k=2)
        est._fmap = f
        result = est.estimate([-60, -60, -60])
        assert "error" not in result
        # Dovrebbe stare a meta' strada circa
        assert 1.0 < result["x"] < 3.0
        assert abs(result["y"] - 2.0) < 1.0

    def test_estimate_confidence(self):
        """Confidenza alta per match esatto, bassa per match lontano."""
        f = self.make_fmap()
        est = PositionEstimator(k=1)
        est._fmap = f

        # Match esatto
        r_exact = est.estimate([-30, -50, -70])
        # Match molto diverso
        r_far = est.estimate([-90, -90, -90])
        assert r_exact["confidence"] > r_far["confidence"]

    def test_estimate_not_ready(self):
        """Estimator non pronto restituisce errore."""
        est = PositionEstimator()
        result = est.estimate([-40, -41, -42])
        assert "error" in result

    def test_save_and_load_estimator(self):
        """Estimator salva mappa e la ricarica."""
        f = self.make_fmap()
        est1 = PositionEstimator(k=3)
        est1._fmap = f

        tmp_path = tempfile.mktemp(suffix=".json")
        try:
            f.save(tmp_path)

            est2 = PositionEstimator()
            est2.load(tmp_path)
            assert est2.ready
            assert est2._fmap is not None
            assert est2._fmap.n_points == 3
            result = est2.estimate([-35, -45, -65])
            assert "error" not in result
        finally:
            os.unlink(tmp_path)

    def test_estimate_empty_map(self):
        """Mappa senza punti restituisce errore."""
        f = FingerprintMap(width=6, height=5)
        est = PositionEstimator()
        est._fmap = f
        est._ready = True
        result = est.estimate([-40, -41, -42])
        assert "error" in result

    def test_estimate_wrong_rssi_length(self):
        """Vettore RSSI di lunghezza errata restituisce errore."""
        f = FingerprintMap(width=6, height=5)
        f.add_point_xy(0, 0, rssi=[-30, -50, -70])
        est = PositionEstimator()
        est._fmap = f
        # 0 punti -> ready=False -> errore (non passa da len check)
        result = est.estimate([-40, -41, -42])
        assert "error" in result

    def test_estimate_wrong_rssi_length(self):
        """Vettore RSSI di lunghezza errata restituisce errore."""
        f = FingerprintMap(width=6, height=5)
        f.add_point_xy(0, 0, rssi=[-30, -50, -70])
        est = PositionEstimator()
        est._fmap = f
        result = est.estimate([-40, -41])  # 2 invece di 3
        assert "error" in result


# ============================================================
# Room mapper CLI (integrazione con processi esterni)
# ============================================================

class TestFingerprintPoint:
    def test_point_creation(self):
        p = FingerprintPoint(x=1.5, y=2.5, rssi=[-40, -41, -42],
                             label="test", timestamp=1000)
        assert p.x == 1.5
        assert p.y == 2.5
        assert p.rssi == [-40, -41, -42]
        assert p.label == "test"

    def test_point_default_timestamp(self):
        p = FingerprintPoint(x=0, y=0, rssi=[-50, -51, -52])
        assert p.timestamp > 0
