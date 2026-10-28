"""서비스에 복사해 둔 스키마가 shared/schemas 원본과 같은지 확인한다."""
import json
from pathlib import Path

import pytest

from c2.schemas import SCHEMA_DIR

SHARED = Path(__file__).resolve().parents[3] / "shared" / "schemas"


@pytest.mark.parametrize("name", ["frame-envelope", "processed-frame"])
def test_vendored_schema_matches_shared(name):
    shared = SHARED / f"{name}.schema.json"
    if not shared.exists():
        pytest.skip(f"shared schemas not available: {SHARED}")
    vendored = SCHEMA_DIR / f"{name}.schema.json"
    assert json.loads(vendored.read_text(encoding="utf-8")) == json.loads(shared.read_text(encoding="utf-8")), (
        f"{vendored} is out of sync with {shared}. Copy the shared schema into services/c2-preprocessing/schemas/.")
