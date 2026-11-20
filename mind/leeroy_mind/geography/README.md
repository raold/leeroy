# Geography

Where the guild's members live, and where he does. Code-built from a data sheet; no model call,
no migration, no native rebuild.

## Switches

```toml
[geography]
enabled = true        # the default: everything below. false: every prompt and clock as before

[owner]
home = "Montana"      # optional: his state or region (a state's name or postal code, DC,
                      # "Scotland", "the North West of England", "UK"...)
home_detail = "Pinecrest"  # optional: a town, at most 40 letters, spaces and . , ' -
```

Both are read once at the mind's start (restart the mind, and the console for its Discord
presence). An invalid `[geography]` table is off. An unknown `home` or a bad `home_detail` is
dropped with one warning that names the key, never the value; nothing logs, shows or stores
where he lives, only `owner_home=set|unset` at start. Keep `[owner]` in the local `mind.toml`
only: never in the repo or a test.

## The place sheet

`places.toml`: the fifty states, the District of Columbia, England's regions, Scotland, Wales,
Northern Ireland and the UK as a whole. For each: its IANA zone, where most of its people live
(distances only), its coast (ocean, lakes, none), whether anything there is a mountain, humid
summers, terrain and notable geography in plain words, its biggest cities, a few everyday
touchstones, the months snow can fall somewhere in it, seasonal events (hurricane season,
tornado season, monsoon storms, wildfire smoke, nor'easters...), a few seasonal notes, and
typical daily highs and lows by month (Fahrenheit for a state, Celsius for the UK) for where
most of its people live. Plain, general facts; a big state's other parts differ, and its terrain
says so.

## Where she lives

- A US member: her home state (`Identity.home`, from `personas homes`). Without one she has no
  place and nothing changes for her (US Eastern, no line).
- A UK member: the region her own words name (the first place in her real-life texts, household
  first, then her mock interview; a relative's place only when she names none of her own; "Welsh"
  and the like only when no place is named), else the UK as a whole.

## What changes with it on

- **Her card**: "You live in Colorado." after who she is, in her own live card only
  (`render_prompt_card(home=...)`). The night's cards and every other reader (scenes, write-ups,
  the critic, world personas, photo prompts) go without it, and no prompt carries another
  member's home: what members said of their homes still travels only as memories, by their
  audiences.
- **Where you live, right now** (`lines.now_parts`, at most 60 estimated tokens): her place and
  its land, the season there (typical highs and lows for the date, how it feels at her hour, snow
  when it can come, the season's event or note) and whether it is light out, just before "Right
  now:" on her private turns (whispers, DMs) and her Discord turns. The first thing an
  over-budget prompt lets go, its last part first.
- **Her clock** (`discord.presence.her_local`): her day plan, Discord presence and status, her
  answers on her own time, her state through the day, the server engine, banter, decisions and
  voice notes, and "Right now" use her home's zone. A naive owner time is US Eastern, as before.
  `photos/` and `selfies/` still read the old region clock (`local_time`).
- **His home** (`[owner] home`): in her whisper or DM to him only, after the never-met line in
  "So far between you two": where he lives, how far that is from her ("about 7 hours' drive", "a
  flight", "an ocean away") and how his clock stands to hers, and while they have never met,
  "Meeting up is only ever a what-if between you." Never in a public, officer, group DM or
  Discord channel prompt.
- **Guards** (`tells.py`): `place`, soft (asked again once with her place; a retry that still has
  it is kept): snow in a month it cannot fall there, a heatwave in a cold month or a hard frost
  in a warm one, a temperature far outside her month's, the sea or mountains her place has none
  of, a clock time or time of day that is not hers; never in a clause about the game, another
  place, him, the past, a plan, a trip, a what-if or indoors. `his place`: a line others read
  that names his home or town in a sentence about him, when the chat shown did not say it already;
  asked again, dropped when the retry still has it, refused for an owed answer's fallback.

`python -m leeroy_mind life places [--guid N] [--at ...]` (read-only) prints each member's place,
zone and local time, and with `--guid` her line. It never prints his home.

## Rollback

`[geography] enabled = false` and restart the mind and the console: no home line, no place
line, no his-home line, the old clocks, no guard. Removing `[owner]` alone takes only his home
out. Older code ignores both tables (it logs them as unknown sections).
