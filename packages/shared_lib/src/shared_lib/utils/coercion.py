def to_int(value: object | None, default: int = 0) -> int:
    """Convert a value to ``int``, returning ``default`` for invalid input."""
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
