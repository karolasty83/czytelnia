"""Pure display labels for physical track indices; no playback or NVDA state."""


def track_label(book, index):
    """Return the display label for a zero-based physical track index."""
    start = book.get('numbering_start', 0)
    if index >= start:
        return f'Odcinek {index - start + 1}'
    return 'Wstęp' if start == 1 else f'Wstęp {index + 1}'
