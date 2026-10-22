"""DeprecationWarning helper for the APIs being retired (client.access)."""

from __future__ import annotations

import functools
import warnings
from contextvars import ContextVar
from typing import Any, Callable, TypeVar

F = TypeVar("F", bound=Callable[..., Any])

# Set while a deprecated method runs, so the deprecated methods it calls
# internally do not warn again.
_in_deprecated_call: ContextVar[bool] = ContextVar("in_deprecated_call", default=False)


def deprecated(api: str, replacement: str) -> Callable[[F], F]:
    """Emit ``"{api}.{method}() is deprecated. {replacement}"`` on each call.

    The warning points at the caller's line. Only the outermost deprecated
    call warns.
    """

    def decorator(func: F) -> F:
        message = f"{api}.{func.__name__}() is deprecated. {replacement}"

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if not _in_deprecated_call.get():
                warnings.warn(message, DeprecationWarning, stacklevel=2)
            token = _in_deprecated_call.set(True)
            try:
                return func(*args, **kwargs)
            finally:
                _in_deprecated_call.reset(token)

        return wrapper  # type: ignore[return-value]

    return decorator
