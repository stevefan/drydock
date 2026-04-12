import pytest

from drydock.core.registry import Registry
from drydock.output.formatter import Output


@pytest.fixture
def registry(tmp_path):
    return Registry(db_path=tmp_path / "test.db")


@pytest.fixture
def output():
    return Output(force_json=True)
