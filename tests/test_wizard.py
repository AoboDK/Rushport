import pytest
from unittest.mock import patch, MagicMock, AsyncMock


# Async generator helper used by transfer tests
async def async_items(*items):
    for item in items:
        yield item


def test_main_is_callable():
    from wizard import main
    assert callable(main)


def test_handle_interrupt_exits_cleanly():
    from wizard import _handle_interrupt
    with pytest.raises(SystemExit) as exc:
        _handle_interrupt()
    assert exc.value.code == 0
