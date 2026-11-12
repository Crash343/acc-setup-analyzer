"""Struct-Layout des ACC Shared Memory (Live-Mitschnitt) gegen die bekannten Offsets prüfen."""
from accanalyzer import shm


def test_layout():
    assert shm.check_layout() == []
