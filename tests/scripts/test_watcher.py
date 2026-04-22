"""Watcher: transition detection is the real contract."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


# watcher.py lives outside the src/ package; load it by path so tests
# don't force a reorg.
def _load_watcher():
    path = Path(__file__).resolve().parents[2] / "scripts" / "watcher" / "watcher.py"
    spec = importlib.util.spec_from_file_location("drydock_watcher", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["drydock_watcher"] = module
    spec.loader.exec_module(module)
    return module


watcher = _load_watcher()


def test_summarize_flattens_multi_desk_payload():
    payload = {
        "desks": [
            {"desk": "infra", "healthy": True, "violations": 0,
             "checks": [{"kind": "job", "name": "sync", "healthy": True}]},
            {"desk": "auction-crawl", "healthy": False, "violations": 2,
             "checks": [
                 {"kind": "output", "name": "/x/db", "healthy": False},
                 {"kind": "job", "name": "daily", "healthy": True},
             ]},
        ],
        "healthy": False, "total_violations": 2,
    }
    state = watcher.summarize(payload)
    assert set(state.keys()) == {"infra", "auction-crawl"}
    assert state["infra"]["healthy"] is True
    assert state["auction-crawl"]["violations"] == 2
    assert "output:/x/db:bad" in state["auction-crawl"]["check_keys"]
    assert "job:daily:ok" in state["auction-crawl"]["check_keys"]


def test_summarize_single_desk_payload():
    """When ws deskwatch <name> is invoked, the payload is a single desk, not
    wrapped in 'desks'. Watcher must handle both shapes."""
    payload = {"desk": "infra", "healthy": True, "violations": 0, "checks": []}
    state = watcher.summarize(payload)
    assert set(state.keys()) == {"infra"}


def test_no_transitions_when_state_unchanged():
    """The sink stays untouched when every desk has the same
    healthy+checks between ticks. Quiet-by-default is the whole point
    of the watcher: it only speaks on state change."""
    s = {"a": {"healthy": True, "violations": 0,
               "check_keys": ["job:x:ok"], "note": None}}
    assert watcher.detect_transitions(s, s) == []


def test_transition_healthy_to_unhealthy_emits_note():
    was = {"a": {"healthy": True, "violations": 0,
                 "check_keys": ["job:x:ok"], "note": None}}
    now = {"a": {"healthy": False, "violations": 1,
                 "check_keys": ["job:x:bad"], "note": None}}
    notes = watcher.detect_transitions(was, now)
    assert len(notes) == 1
    assert "UNHEALTHY" in notes[0]


def test_transition_unhealthy_to_healthy_emits_recovery():
    was = {"a": {"healthy": False, "violations": 2,
                 "check_keys": ["job:x:bad"], "note": None}}
    now = {"a": {"healthy": True, "violations": 0,
                 "check_keys": ["job:x:ok"], "note": None}}
    notes = watcher.detect_transitions(was, now)
    assert "recovered" in notes[0]


def test_transition_desk_appeared():
    notes = watcher.detect_transitions(
        {},
        {"new-desk": {"healthy": True, "violations": 0,
                      "check_keys": [], "note": None}},
    )
    assert "appeared" in notes[0]
    assert "new-desk" in notes[0]


def test_transition_desk_disappeared():
    notes = watcher.detect_transitions(
        {"gone": {"healthy": True, "violations": 0,
                  "check_keys": [], "note": None}},
        {},
    )
    assert "disappeared" in notes[0]


def test_check_set_changed_without_overall_healthy_flip():
    """A desk can pick up or lose a specific violation while staying
    unhealthy overall. We still want a note because the incident shape
    changed — useful when a new output starts failing while an old one
    also still fails."""
    was = {"a": {"healthy": False, "violations": 1,
                 "check_keys": ["output:db:bad"], "note": None}}
    now = {"a": {"healthy": False, "violations": 2,
                 "check_keys": ["output:alerts:bad", "output:db:bad"],
                 "note": None}}
    notes = watcher.detect_transitions(was, now)
    assert len(notes) == 1
    assert "check set changed" in notes[0]
