"""Upload counts and user-facing refusal text, shared by console and mind."""
DAY_MAX = 200
MINUTE_MAX = 4
DAY_KEY = 'photos:uploads:{}'
WORDS = {
    'not_jpeg': 'Choose a JPEG photo.', 'too_big': 'That photo is over 4 MiB.',
    'bad_size': 'Each side of a photo must be 16 to 8192 pixels.',
    'bad_token': 'Choose the photo again: its upload token is invalid.',
    'missing': 'That uploaded photo is missing. Choose it again.',
    'link': 'That photo path is a link and cannot be used.',
    'reused': 'That upload was already used. Choose the photo again.',
    'busy': 'The photo could not be staged. Try again.',
    'minute': 'Wait a minute before posting another photo (four per minute).',
    'day': 'Today\'s 200-photo allowance is used up.',
    'off': 'Photo uploads are off in this mind.',
}


def day_count(conn, day: str) -> int:
    row = conn.execute('SELECT value FROM meta WHERE key=?', (DAY_KEY.format(day),)).fetchone()
    try:
        return max(0, int(row[0])) if row else 0
    except (ValueError, TypeError):
        return DAY_MAX  # a broken counter never grants another allowance
