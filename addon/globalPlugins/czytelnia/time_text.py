"""Shared Polish time descriptions, independent of NVDA."""


def plural(number, singular, few, many):
    """Choose a Polish noun form for a nonnegative integer count."""
    if number == 1:
        return singular
    if 2 <= number % 10 <= 4 and not 12 <= number % 100 <= 14:
        return few
    return many


def format_time(seconds, *, accusative=False):
    total = max(0, int(seconds))
    if total < 60:
        return '{} {}'.format(total, plural(total, 'sekundę' if accusative else 'sekunda', 'sekundy', 'sekund'))
    hours, remainder = divmod(total, 3600)
    minutes = remainder // 60
    parts = []
    if hours:
        parts.append('{} {}'.format(hours, plural(hours, 'godzinę' if accusative else 'godzina', 'godziny', 'godzin')))
    if minutes:
        parts.append('{} {}'.format(minutes, plural(minutes, 'minutę' if accusative else 'minuta', 'minuty', 'minut')))
    return ' i '.join(parts)
