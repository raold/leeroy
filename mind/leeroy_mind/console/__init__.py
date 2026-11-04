"""The guild console: one local browser window for the guild's voice rooms, its text, and the
mind and voice services. `python -m leeroy_mind console --data-dir <mind-data>` runs it in a
process of its own, so it keeps working, and can start the mind again, while the mind is down.

It listens on 127.0.0.1 only (port 7881 by default) and gates every request with a key made
when it starts (in the printed URL's fragment, then a header from the page's memory; never a
cookie) and an Origin check; see app.py. It reads
the mind's data read-only: mind.db, mind.log and mind.toml. Starting and stopping go through
leeroy_mind.lifecycle, the rules scripts/ac-mind.py uses; --no-control turns them off. A stop
is the mind's own clean shutdown when the mind's control endpoint (leeroy_mind.endpoint)
answers, and the Raid tab's actions go through that endpoint too (mindlink.py)."""

DEFAULT_PORT = 7881
VOICE_UI_PORT = 7880  # the voice service's page socket, unless its voice.toml says otherwise
