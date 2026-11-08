# Tuning and temporary nudges

The Personas tuning panel controls the owner's dials and temporary nudges, for a
member or a saved tuning group. The stored persona is never changed. A nudge is
1–200 cleaned characters, light or firm, lasting one hour to fourteen days.
Choose Everywhere, Game only, Discord only, or Whispers with you only. Retire it
early, undo a change, or revert tuning to an earlier change. Group membership is
evaluated from the current stored persona and roster, as it is for group dials.

The panel shows active nudges, time remaining, inherited group nudges, and folded
history. The wall marks members who have active nudges. Unsent drafts stay in
page memory through polling; they are not saved in browser storage. Reloading
discards them. Preview shows a sample before and after, only while the live model
is already loaded. It never loads a model, stores a line, or broadcasts it.

Nudges are intentions, not scripts or evidence of events. Their wording is framed
as a temporary state of mind beneath character, factual, safety and privacy
rules. A firm nudge has priority within the prompt budget. No nudge bypasses the
normal output checks, changes a claim, or promises that the model will follow it.

## Storage and undo

No migration is added. The existing `meta` table holds current projections under
`tuning:nudge:value:<id>` and append-only before/after receipts under
`tuning:nudge:change:<change>`. The receipt and its ordinary `tuning_change` row
commit atomically. IDs share the dial history. Undo checks that the affected
nudge still has the state left by that change; revert folds both histories in one
transaction. Undo does not extend an expired end time. Every nudge's author is
`owner`; text is private database content, never a log field.

Read active nudges and history with:

```sh
python -m leeroy_mind tuning --data-dir D nudges
python -m leeroy_mind tuning --data-dir D undo 42
python -m leeroy_mind tuning --data-dir D revert 41
```

Undo and revert require the mind to be stopped, hold its instance lock, and take
a snapshot first. The running mind accepts changes through the existing `tune`
control op. Its new form has `action: "nudge"` and `verb: "set" | "retire" |
"list" | "preview"`. Set and preview take `target_kind`, `target`, `text`,
`strength`, `scope` and `seconds`; retire takes `id`. Console requests retain the
key, Host, Origin and `--no-control` gates, and previews share the existing lock,
deadline and GPU lease. Text follows Board reply cleaning and the real-name
screen; configured owner and persona real names are refused without echoing them.

## Prompt and night behavior

The existing tuning entry is `talk.prompt.comms_rules`, called during
`Pipeline._compose`. Its call now passes the current time, channel and recipient.
The private overlay wrapper carries nudge records; every composition rechecks
expiry and scope even when the persona is cached. Game scope accepts game chat
channels only. Discord scope accepts `discord:<channel>` and `dm:<member>`.
Whispers scope also requires the recipient to be the owner. A call with no channel
context adds no nudge, so card, critic and other readers do not inherit scoped
instructions. With no nudge active, prompts remain byte-identical.

Whole nudge lines share a 200 estimated-token budget, firm first and then newest;
ones that do not fit are omitted rather than cut. Up to 64 nudges may be active
across the guild. The panel lists all active records, including ones omitted by
the prompt budget. It does not claim that a nudge was acted on.

```toml
[tuning]
nudge_memories = true
```

This defaults to true. When an everywhere nudge ends, reflection may consider it
as a past mood, never evidence that an event happened or that another person felt
or acted a certain way. At most two ended intentions from the night's window are
offered. Reflection's existing grounding and privacy checks still decide whether
anything is remembered; no memory is forced. Scoped nudges are excluded so a
private/channel-limited intention cannot escape its scope through later recall.
An invalid tuning table disables these hints. Set false to omit all such hints.

## Isolated validation

```sh
python -m pytest -q tests/tuning tests/console/test_console_nudges.py
node --test tests/console/js/nudges.test.mjs
python -m pytest -q tests/console/test_nudges_browser.py
```

The browser test uses invented members, a fake-model mind and a scratch console
on a free loopback port. It skips explicitly when Playwright or Chromium is absent.
