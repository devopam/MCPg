"""Shared test doubles for unit tests."""


class FakePool:
    """Stand-in for the vendored DbConnPool that records lifecycle calls."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.connect_calls = 0
        self.close_calls = 0
        self._is_valid = False

    async def pool_connect(self, connection_url: str | None = None) -> object:
        self.connect_calls += 1
        if self.fail:
            raise ValueError("connection refused")
        self._is_valid = True
        return object()

    async def close(self) -> None:
        self.close_calls += 1
        self._is_valid = False

    @property
    def is_valid(self) -> bool:
        return self._is_valid
