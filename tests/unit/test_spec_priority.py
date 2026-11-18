import pytest
from pydantic import ValidationError

from sca.kinds.coding.spec import CodingSpec


def test_worker_accepts_the_stored_priority():
    # The API and the eval runner store the admission class in the spec the worker validates.
    spec = CodingSpec.model_validate({"repo_url": "https://example.com/r.git", "instruction": "x",
                                      "priority": "eval"})
    assert spec.priority == "eval"
    with pytest.raises(ValidationError):
        CodingSpec.model_validate({"repo_url": "https://example.com/r.git", "instruction": "x",
                                   "priority": "urgent"})
