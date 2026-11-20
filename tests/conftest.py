import pytest


@pytest.fixture
async def best_of_repo(tmp_path):
    from tests.best_of_harness import make_repo

    return await make_repo(tmp_path / "repo")
