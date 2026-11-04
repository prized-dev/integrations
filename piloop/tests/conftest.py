"""No offline test reaches the real Prized API: without ``PRIZED_LIVE=1`` the key is gone from this process for good,
so a thread that a test leaves running finds no key either."""

import os

if os.environ.get('PRIZED_LIVE') != '1':
    os.environ.pop('PRIZED_API_KEY', None)
