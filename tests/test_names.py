"""Golden tests for the release-name parser (plan Appendix A). Shared fixtures live in names.yaml."""
from pathlib import Path

import pytest
import yaml

from catalog.names import parse_release

CASES = yaml.safe_load((Path(__file__).parent / "fixtures" / "names.yaml").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", CASES, ids=[c["name"][:50] for c in CASES])
def test_parse(case):
    p = parse_release(case["name"])
    for key, want in case["expect"].items():
        assert getattr(p, key) == want, f"{key}: got {getattr(p, key)!r}, want {want!r}"
