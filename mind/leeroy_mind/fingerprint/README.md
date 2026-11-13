# Typing fingerprint, version 1

`[fingerprint] enabled = true` in `mind.toml` attaches the nightly `fingerprint`
stage after thread extraction, with no model. A restart applies the switch;
missing, false or malformed settings leave the stage unattached. Manual
`nightly run` uses the same switch. Optional [learning application](../learn/README.md#applying-learning-to-live-talk)
uses an audience-filtered numeric hint for typing arms; the measurement switch
alone does not change prompts. `python -m leeroy_mind fingerprint show 101 --data-dir <directory>`
opens SQLite read-only, with no migration, service, model or logger setup.

The latest 300 mind-origin lines per member are ordered by timestamp, then id.
Word counts use English alphabetic words with internal apostrophes, after WoW
markup is stripped; marks and emoji do not count as words. p25/p50/p90 interpolate
at `(n-1)*q`. Rates are fractions of lines containing the feature. Lowercase means
the first word starts lowercase; omission means the trimmed line lacks final
`.!?;:,…`; caps emphasis means a word longer than one letter is uppercase.
Known-typo rate alone is a fraction of words: a small fixed list of common
misspellings gives a lower bound, not a general dictionary-based spelling score.
Slang and laughter use the explicit small lists in `__init__.py`.

Below 50 lines, the entire profile is a deterministic prior, labelled `seeded=1`.
It is an authored starting point, not a claim about real demographic populations.
Let `t` be the effective talkative dial (0–10), `h` the humour dial divided by 5,
`a` age clamped to 25–99, `o=(a-25)/74`, and `e` 2 for an exact education value
`university`/`postgraduate`, 1 for `college`, otherwise 0. Missing age is 35; dials
default to 5. Missing/unknown education is neutral; no inference is made from jobs.

| Feature | Prior |
| --- | --- |
| Median words | `6+t+(a-35)/10+e` |
| p25 / p90 | `.55 / 1.8` times median |
| Lowercase | `.6-.3*o-.03*e` |
| Omit punctuation | `.65-.3*o-.05*e` |
| Emoji / emoticon | `.04-.03*o` / `.05+.01*t` |
| Questions / caps emphasis / slang | `.1+.01*t` / `.02+.01*t` / `.08+.01*t` |
| Exclamations | `.05+.01*t`, plus `.02` for US |
| Laughter | humour base plus `.02` for UK, times `h`, capped at 1 |
| Known typos | 0, unmeasured prior |

Laughter bases: dry/dark .06, self-deprecating .10, teasing .14, puns .12,
absurd .18, otherwise .10. Lengths round to two decimal places and other priors
to four. A standalone read before any saved profile uses neutral dials; the night
uses the member's effective dials, including group offsets.

Profiles contain numbers and phrase ids only. Private/officer lines contribute
numeric measurements, never lexical opener/closer text. Those n-grams (1–3 words)
come only from undirected guild/say/yell lines; known character and persona real
names and the owner's real name are screened. Top five per kind use frequency,
then alphabetical tie-breaking. Emoji/emoticon marks and fixed laughter tokens
may be counted in any channel. The private database retains at most 25 current
phrase rows per member; retired ids are never reused. No phrase text is exported
by the CLI or hint renderer, and logs contain only codes, ids and counts.

`fingerprint(conn, guid)` returns the saved version-1 profile or a persona prior
without writing, including on older databases. `available=0` means no usable
profile/persona. The fixed-word `render_style_hint(profile)` is at most 160
characters (40 tokens under the repository estimator) and never resolves phrase
ids, names or authored text.

Enabled weekly reports add the current profile snapshot's mean pairwise L1
distance over three quantiles (divided by 60, capped at 1) and ten rates, with equal
feature weight. Phrase ids are excluded: their numeric difference has no meaning.
Seeded and missing counts stay visible; fewer than two available profiles yields
null, not zero. This is a style-spread indicator, not a held-out-week measurement
or proof that conversational quality improved.
