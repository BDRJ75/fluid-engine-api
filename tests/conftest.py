import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def pytest_addoption(parser):
    parser.addoption("--update-golden", action="store_true", default=False,
                     help="Regenerate tests/golden/*.json instead of comparing")


@pytest.fixture
def update_golden(request) -> bool:
    return bool(request.config.getoption("--update-golden")) or os.environ.get("UPDATE_GOLDEN") == "1"
