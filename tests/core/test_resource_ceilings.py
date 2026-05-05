"""Tests for HardCeilings parsing + docker-runArgs translation.

These pin the Phase A contracts that aren't obvious from the code:

- Empty/missing resources_hard → empty HardCeilings (no flags emitted).
  Phase A is opt-in per desk; absent ceiling = substrate default.
- Memory size strings normalize to docker's expected form. Both docker-
  native ("4g") and IEC-suffixed ("4Gi") inputs accepted.
- Each field optional; partial declarations work (just memory_max, just
  cpu_max).
- Validation rejections raise ResourceCeilingError with the field name
  in the message — that's the user-facing contract for fix: hints.
- bool sneak-throughs (True is technically int) explicitly rejected on
  cpu_max, pids_max, memory_max.
- to_docker_runargs preserves declaration order and uses the existing
  overlay convention (single-string flags with `=`).
"""

from __future__ import annotations

import pytest

from drydock.core.resource_ceilings import (
    HardCeilings,
    ResourceCeilingError,
)


class TestHardCeilingsParsing:
    def test_empty_dict_yields_empty_ceilings(self):
        hc = HardCeilings.from_dict({})
        assert hc.is_empty() is True
        assert hc.to_docker_runargs() == []

    def test_none_yields_empty_ceilings(self):
        hc = HardCeilings.from_dict(None)
        assert hc.is_empty() is True

    def test_full_declaration_round_trip(self):
        hc = HardCeilings.from_dict(
            {"cpu_max": 2.0, "memory_max": "4g", "pids_max": 512}
        )
        assert hc.cpu_max == 2.0
        assert hc.memory_max == "4g"
        assert hc.pids_max == 512
        assert hc.to_docker_runargs() == [
            "--cpus=2.0", "--memory=4g", "--pids-limit=512",
        ]

    def test_partial_declaration_works(self):
        # Just memory — common for ML desks where cpu/pids defaults are fine.
        hc = HardCeilings.from_dict({"memory_max": "8g"})
        assert hc.to_docker_runargs() == ["--memory=8g"]

    def test_unknown_keys_rejected(self):
        with pytest.raises(ResourceCeilingError, match="unknown.*resources_hard"):
            HardCeilings.from_dict({"cpu_max": 1.0, "swap_max": "2g"})


class TestMemoryNormalization:
    @pytest.mark.parametrize("input_str,expected", [
        ("4g", "4g"),
        ("4G", "4g"),
        ("4Gi", "4g"),
        ("4GiB", "4g"),
        ("256m", "256m"),
        ("256Mi", "256m"),
        ("1024k", "1024k"),
        ("1024Ki", "1024k"),
    ])
    def test_size_strings_normalize_to_docker_form(self, input_str, expected):
        # IEC ("4Gi") and docker ("4g") both accepted, normalize to docker form.
        hc = HardCeilings.from_dict({"memory_max": input_str})
        assert hc.memory_max == expected

    def test_raw_int_treated_as_bytes(self):
        hc = HardCeilings.from_dict({"memory_max": 4_000_000_000})
        assert hc.memory_max == "4000000000b"

    def test_zero_rejected(self):
        with pytest.raises(ResourceCeilingError, match="must be > 0"):
            HardCeilings.from_dict({"memory_max": "0g"})
        with pytest.raises(ResourceCeilingError, match="must be > 0"):
            HardCeilings.from_dict({"memory_max": 0})

    def test_empty_string_rejected(self):
        with pytest.raises(ResourceCeilingError, match="cannot be empty"):
            HardCeilings.from_dict({"memory_max": ""})

    def test_unrecognized_suffix_rejected(self):
        with pytest.raises(ResourceCeilingError, match="not a recognized size"):
            HardCeilings.from_dict({"memory_max": "4xyz"})

    def test_bool_rejected(self):
        with pytest.raises(ResourceCeilingError, match="bool"):
            HardCeilings.from_dict({"memory_max": True})


class TestCpuValidation:
    def test_int_accepted(self):
        hc = HardCeilings.from_dict({"cpu_max": 2})
        assert hc.cpu_max == 2.0

    def test_float_accepted(self):
        hc = HardCeilings.from_dict({"cpu_max": 0.5})
        assert hc.cpu_max == 0.5

    def test_zero_rejected(self):
        with pytest.raises(ResourceCeilingError, match="must be > 0"):
            HardCeilings.from_dict({"cpu_max": 0})

    def test_negative_rejected(self):
        with pytest.raises(ResourceCeilingError, match="must be > 0"):
            HardCeilings.from_dict({"cpu_max": -1.0})

    def test_string_rejected(self):
        with pytest.raises(ResourceCeilingError, match="positive number"):
            HardCeilings.from_dict({"cpu_max": "2.0"})

    def test_bool_rejected(self):
        # True is technically int — exclude before generic int check or
        # `cpu_max: true` would silently become 1.0.
        with pytest.raises(ResourceCeilingError, match="bool"):
            HardCeilings.from_dict({"cpu_max": True})


class TestPidsValidation:
    def test_int_accepted(self):
        hc = HardCeilings.from_dict({"pids_max": 256})
        assert hc.pids_max == 256

    def test_zero_rejected(self):
        with pytest.raises(ResourceCeilingError, match=">= 1"):
            HardCeilings.from_dict({"pids_max": 0})

    def test_float_rejected(self):
        with pytest.raises(ResourceCeilingError, match="positive integer"):
            HardCeilings.from_dict({"pids_max": 256.0})

    def test_bool_rejected(self):
        with pytest.raises(ResourceCeilingError, match="positive integer"):
            HardCeilings.from_dict({"pids_max": True})


class TestDockerRunArgsTranslation:
    def test_flag_format_uses_equals(self):
        # Match existing overlay convention (--hostname=foo, not --hostname foo)
        hc = HardCeilings.from_dict({"cpu_max": 1.5})
        args = hc.to_docker_runargs()
        assert args == ["--cpus=1.5"]
        assert all("=" in a for a in args)

    def test_declaration_order_preserved(self):
        # cpu, memory, pids — same order as the dataclass / docker convention
        hc = HardCeilings.from_dict({
            "memory_max": "1g", "pids_max": 100, "cpu_max": 0.5,
        })
        args = hc.to_docker_runargs()
        assert args == ["--cpus=0.5", "--memory=1g", "--pids-limit=100"]


class TestRoundTripViaToDict:
    def test_to_dict_omits_unset_fields(self):
        hc = HardCeilings.from_dict({"memory_max": "4g"})
        assert hc.to_dict() == {"memory_max": "4g"}

    def test_to_dict_round_trips_through_from_dict(self):
        original = {"cpu_max": 2.0, "memory_max": "4g", "pids_max": 512}
        hc = HardCeilings.from_dict(original)
        assert HardCeilings.from_dict(hc.to_dict()) == hc
