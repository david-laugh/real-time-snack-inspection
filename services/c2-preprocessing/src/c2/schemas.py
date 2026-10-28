"""shared/schemas 사본(services/c2-preprocessing/schemas/)으로 메시지를 검증한다.

compose의 build context가 이 서비스 디렉터리라 컨테이너 안에서는 shared/를 읽을 수 없어서 사본을 둔다.
원본과 내용이 같은지는 tests/test_schema_sync.py가 확인한다.
"""
import json
import os
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator

SCHEMA_DIR = Path(os.environ.get("C2_SCHEMA_DIR", Path(__file__).resolve().parents[2] / "schemas"))


@lru_cache
def validator(name: str) -> Draft202012Validator:
    schema = json.loads((SCHEMA_DIR / f"{name}.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def errors(name: str, message: dict) -> list[str]:
    """스키마 위반 목록. 비어 있으면 통과."""
    return [f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message[:200]}"
            for e in validator(name).iter_errors(message)]
