"""Small shared helpers."""
import logging
import time

log = logging.getLogger("notesgraph")
_RETRYABLE = (
    "429",
    "rate limit",
    "rate_limit",
    "quota",
    "resource_exhausted",
    "503",
    "unavailable",
    "500",
    "timeout",
    "readerror",
    "connection",
    "winerror 10035",
    "temporarily",
    "overloaded",
)


def with_retry(
    fn, tries: int = 5, base_delay: float = 3.0, max_delay: float = 30.0
):
    """Call fn(); retry with exponential backoff on rate-limit / transient errors."""
    for attempt in range(tries):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            msg = str(exc).lower()
            if attempt == tries - 1 or not any(k in msg for k in _RETRYABLE):
                raise
            delay = min(base_delay * (2**attempt), max_delay)
            log.warning(
                "Transient provider error; retrying attempt %d/%d in %.0f seconds: %s",
                attempt + 1,
                tries - 1,
                delay,
                str(exc)[:240],
            )
            time.sleep(delay)
