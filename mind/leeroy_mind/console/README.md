# Guild console guide

Open your private keyed bookmark. Comms opens first. Tonight is the first tab in the strip. Choose **?** on any tab for that tab's guide, the console version and the local checkout commit. Help keeps the running console’s startup guide, version and source commit. About checks the managed checkout’s current commit each time. If it advances while the console stays open, restart the console and verify the running identity. An installed package can report its version even when its source commit is unavailable.

## Opening the console on a phone

1. On the installation PC, start the mind environment's `python -m leeroy_mind console`. Its default listener is `127.0.0.1:7881`. Open the URL printed at startup. For a persistent bookmark, start with `--key-file`. The default file is `%LOCALAPPDATA%\leeroy\console.key`. An explicit path must be a regular owner-only file. The console refuses unsafe ownership, permissions and links.
2. For phone access, connect both devices to the owner's tailnet. On the PC, point Tailscale Serve's private HTTPS endpoint to the console, for example `tailscale serve --bg --https=443 http://127.0.0.1:7881`. Check its status. Remove just this mapping with `tailscale serve --https=443 off`. Keep Funnel off. Keep the console, voice and mind control listeners off the LAN and public internet.
3. Add the PC's exact MagicDNS name with `--allow-host <pc>.<tailnet>.ts.net` or set `allow_hosts = ["<pc>.<tailnet>.ts.net"]` under `[console]` in `mind.toml` and restart the console. The name has no scheme, port, wildcard or path. For unattended startup use the logon task in the release checklist. Open the printed HTTPS URL on the phone. Treat its fragment as a password.
4. A per-start key changes on every console start. The page consumes the fragment into memory and removes it from the address bar. After a reload, a new tab or a restart, reopen the appropriate keyed bookmark. Only the startup/rotation output should display the keyed URLs. Keep them out of screenshots, logs, tickets and shared or synced bookmarks.
5. To rotate a stable key, run `python -m leeroy_mind console rotate-key --key-file --allow-host <pc>.<tailnet>.ts.net` privately. If the installation uses a custom file, supply its path after `--key-file`. Restart the identified console, replace the bookmark on each trusted device and verify the old bookmark's authenticated reads are refused.

The shell and static files can load without a key. API reads require the `X-Console-Key` header. The browser keeps no key cookie. Writes additionally require the matching Origin and operation-specific gates. The local HTTP and tailnet HTTPS origins cannot cross. The console checks Host and site, ignores forwarded-host claims and serves its own scripts and styles. The separate private mind control endpoint verifies the running process. Never copy its credentials into the browser. Most historical panels use read-only SQLite. Changes to guild data go through the mind.

## Reading status and using controls

On a narrow screen swipe the status strip and the tab row separately. In Comms, **Channels** opens the drawer. Arrow keys move between tabs. A member's face or linked name opens Personas. Back restores the previous view. A hidden page closes its live stream and checks bounded unread counts. **Notify** needs browser permission and shows a sender without the message. **Hide photos** and the theme are local preferences. [Navigation and phone layout](NAVIGATION.md)

Read the source and timestamp beside the number. **Not set up yet** points to an absent layout, schema or feature. A successful read with no rows means nothing was recorded for that period. A stale card retains its last successful value. Bring its tab into view and check that its timestamp advances. The Mind strip distinguishes a running process, bridge connection and actual owner presence. Unknown presence blocks actions that require an offline owner. `--no-control` keeps the console read-only.

**Free GPU** shows its availability and refusal reason. A running mind requires confirmed absence and releases only eligible leases. A successful pause persists while unloading finishes. Check resident models and the pause in Tonight or Ops. A partial unload can be retried. Resume, a WoW login or the owner's Discord post clears the pause. With the mind stopped, a permitted direct unload creates no mind pause. Other GPU users can still hold memory. [GPU controls](GPU.md)

**Render selfies** previews eligible first sets, requested extra sets and planned photos, with counts and an estimate when available. It needs fresh mind status, selfies enabled, a queue and an offline owner. The quiet window, owed replies, protected leases, a night in progress or the GPU pause can block it. A new lease, login or Free GPU can interrupt it. Check the finished count, remaining queue and outcome in Ops. Stop can take time. The Ops booth action has its own admission rules. [Manual render](RENDER.md)

Private whispers and DMs stay with their pair, group DMs with participants and officer channels with officers. Discord activity never supplies game login, AFK or session evidence. Logs carry IDs, counts, reason codes, scores and timings. Keep conversation text, persona content, prompts, names, keys and private media out of reports. Adult image subjects must be 25 or older, clothed and non-explicit. Keep the real-name, likeness and NSFW screens in place for manual work as well.

## Tonight

Start here for raid signups, items waiting for you, DKP due, low bank stock, your raid kit, the next seven days and presence or last night's activity. Open the relevant tab to act. Each card reads its own source. A missing Decisions table, no configured character and an unavailable armory have different remedies. A retained kit shows its age. After login or logout, wait for an observed presence change and a fresh card read. The waiting count keeps message and letter contents private. [Tonight cards](TONIGHT.md)

## Comms

Choose the medium and audience before sending. WoW guild, officer, group and whisper replies need a verified running mind, connected bridge, known in-game owner and a permitted channel or member. Whisper only to an online cast member other than yourself. A group reply goes to party or raid as appropriate. The game must advertise `owner_say`. Slash commands are refused. One line can be in flight at a time. Read the refusal and keep the editable draft. Use Load earlier for older lines. Limited browser windows and counts leave durable conversation history intact. Voice rooms show the voice link, participants, mute/deafen, level and available transcripts. Deafen also mutes your input. Both reset at login. A muted/deafened voiced whisper can appear as unread text with a reason. Recheck the room after a reconnect.

The guild's Discord is built into the console. Open a channel, DM or group DM, reply in a thread, react, pin or use the available edit/delete controls. The mind enforces membership and author permissions. Attachments, photos and voice notes have separate enablement and audience checks. Discord posts can work while logged out of WoW. A DM and an in-game whisper are separate conversations. [Guild Discord server](../server/README.md)

With member heroics enabled, an offer in `#lfg-and-alts` can start once per local day while the owner is known away. It expires in an hour. One level-80 tank, one healer and three damage dealers need four distinct surviving guarded posts from the final roster generation. Each replacement must post. A short conversation can finish as talk only. Driver preparation reserves no bodies and can retry up to eight times at intervals of at least 60 seconds. An accepted original start, bound Halls of Lightning heroic instance and fresh five-member heartbeat establish **Playing — Halls of Lightning**. The display expires after 45 seconds or loss of the link.

A terminal preserves native counts for matching later status. Run evidence can include additional deduplicated encounters recorded for the same start, copy and time. Memories report supported counts as lower bounds. A boss fact goes only to its witnesses as an opener. When witnesses split and nobody knows the aggregate, individual encounters remain available to their respective witnesses. Login alone lets an accepted run continue. See Realm for handback and Ops for unresolved returns.

## Raid

Read the call, composition, attendance, drops, DKP and recent runs. Stored history remains readable when the mind is down. Use Call, Form, Summon, Start, Stop or End only when offered. Check the new phase or group after each action. The lead selector requires compatible native support, an eligible owner/group and an available lead. Formation, an offline hold or an existing raid lead can block it. Stopping dungeon-clear during a run and closing the raid ledger are separate actions. An admitted overlapping summon can wait for day-plan or heroic handback. Recheck the original summon's own result and current combat state.

## Guild

Read the roster, attendance, DKP, loot, awards and chronicle. Open **Decisions** to see who settled a lineup, loot rule, bank request, boss strategy or rank, its state and what actually changed. A missing schema needs migration. With the feature off, existing history can remain. Voting needs an open eligible poll, an unused vote, enabled controls and a verified mind. The vote is final. An officer poll keeps its audience. Read the overrule/undo preview before confirming.

A lineup can direct raid formation. A new loot rule waits for a safe raid boundary. Once a withdrawal completes the stock is in the officer's bags. Mail in transport may still complete after overrule. Confirmed posting proves mailbox delivery. Only a first attachment GUID is available for audit. Overrule blocks future transfers and cannot recall a posted item. A late exact job updates the evidence without sending again. A strategy change can wait for a safe muster or cleanup. Rank restoration waits for roster synchronization. Verify the in-play pull receipt or roster rank after a scheduled change.

Enable `[server] enabled = true` and `[decisions] enabled = true`, then restart mind and console. Use `Leeroy.GuildAdd.FollowRoster` for the lineup picker, `Leeroy.Decisions.BankMail.Enable` for item-only member mail and independently `Leeroy.Decisions.BankRestock.Enable` for approved top-ups. Apply native keys with `.reload config` and reconnect for a fresh capability greeting. Bank permissions, Realm, the whitelist and armory still apply. Strategy also needs native training and mind telemetry/action. Rank zero and the owner are protected. [Decision rules and switches](../decisions/README.md)

## Social

Read the observed mood, ties toward the owner, feuds, jealousy, romances, breakups, rumours and threads. Open a face for its member page. These panels read stored history. Check the particular table and observation night when a panel is empty or stale. A recorded thread is an observation and can exist before any change takes effect.

## Personas

Open the wall or table, select a member, and use Message or Whisper to reach the right conversation. Inspect memories, ties, voice, photo history and Insight. Pick a retained eligible photo, request four more, or undo an offered pick. A stopped mind or disabled control leaves the images readable and the write controls unavailable. Insight's **Check line** can run with editing disabled, but needs a compatible verified mind and a resident embedding model. Under **Talk quality**, check the date of the latest readable audit. Guard counts are unavailable because individual verdicts are never stored. [Insight](INSIGHT.md)

Use Tuning to distinguish the world default, combined group offsets and the member's pin. A member pin wins over summed group offsets. World dials override configuration without rewriting it. Review the effective value and version, preview, then save. Previews need an already loaded model and keep the text out of storage and logs. A later persona revision can leave a pin stale. Check the change log for the actual write, Undo or Revert. For a private nudge enter 1–200 cleaned characters, select a member or group, scope, light/firm strength and an expiry from one hour to 14 days. Verify the active nudge and end time. A preview illustrates the prompt and cannot promise the member's next action. An expired everywhere nudge may reach reflection as a past mood when `[tuning] nudge_memories` is enabled. Scoped nudges stay out of reflection. Godmode edits and strings have their own feature gates. Day-plan activities edit planner preferences. A physical Join my group or Come to me request must still pass the native ownership, combat and instance checks. [Tuning](../tuning/README.md) and [member activities](../godmode/README.md)

## Board

Read public forum threads, your delivered letters and write-ups. Reply to a post or letter, or withdraw a waiting reply. Saving requires control, a running mind, Realm enabled and the reply schema. Forum replies allow up to 200 cleaned characters and letters up to 400. One pending reply holds its target. Rivals may read a public post. Only the correspondent sees a letter reply. Check the nightly disposition for waiting, withdrawn, answered, posted without an answer or refused. Stopping Realm can leave earlier history visible.

## Calendar

Inspect the two-week event calendar, signups, your status and the last 28 days of missed raid nights. Open a participant to see their profile. Check local dates and distinguish pending publication, a posted event and an empty schedule. This tab reads stored signups. Member day blocks and heroic offers have separate records.

## Bank

Read gold, purchased tabs and slots, the money log and the officer movement ledger. Compare the snapshot time with the recorded outflow. A live snapshot needs the verified mind's compatible bank operation and armory read. With the mind stopped, durable movements can still be inspected. An unpurchased tab, an empty purchased tab and an unavailable snapshot are distinct. An item ID may appear while its name is unavailable. Keep database credentials with the mind.

For a guild request, follow Decisions on Guild. Daily caps, entitlement, last-stack protection and fresh stock constrain transfers. The withdrawal and the mail have different direct receipts. One confirmed withdrawal creates one outflow. A successful terminal mail proves posting and the first attachment's item-instance GUID. Historical unverified mail numbers are hidden. When evidence is unknown, preserve the original command and check for its matching late result.

## Realm

Inspect rival guilds, world members, progression, first kills, poaching, Trade and canon grants. Expand or page the world cards. Empty approved rivals, no generated members and a read failure have different explanations. This is a read-only tab.

For day plans, `[life] day_plans = true` enables the night's planning. Physical activity additionally needs `[realm] enabled = true`, `Leeroy.DayPlans.Enable = 1`, `Leeroy.Realm.Enable = 1` and a fresh `day_plan_handoff_v1` greeting. The planner can assign two to four blocks from 08:00 into 02:00 on the following server day (America/New_York). Posted raids reserve the remainder after their start. Company needs matching rows and a witness at the destination. The away cap is 20. An offline block only schedules time. Look for matched native step and arrival/activity evidence before crediting travel, company or fishing.

Day activities can continue with the owner in game. A validated owner add, raid formation or game string can preempt the whole overlapping block. An eligible offline raid can also preempt it. A mixed day/heroic handoff keeps the original request and one 30-second deadline. Handback and the revalidated original command each need their own evidence. A lost check acknowledgement leaves the original unsent. Restart expires waiting originals. Switch-off revokes open permits. Same-boot recovery checks the exact permit. After a native reboot, a dispatched unknown retains its hold. Recovery never replays the body command. See Ops for eligible exact-root abandonment. [Day-plan and heroic recovery](../godmode/README.md)

## Training

Read weekly fights, what hurt the group, the overhead gate, trials, tactics and playbooks. Check the boss, difficulty, fight kind and sample period. A missing schema, an empty period and an insufficient baseline are separate states. Adaptation requires a passed overhead gate and at least 28 days of eligible evidence. Dummy requested damage is a proxy. An offline trial can establish a tactic there. Adoption in a human raid does not prove a performance improvement there. Guild strategy changes need an eligible safe muster. Check the played decision ID and compare matching pull cohorts. On disable or restart, keep training journals and wait for verified cleanup before reusing a body.

## Dojo

Select the isolated Dojo layout to inspect build and baseline receipts, gates, runs, episodes, replays, scenarios and the G1 report. A missing layout, inaccessible file or not-yet-run stage has its own state. Check the named run and measured gate. A listening process alone proves only reachability. Keep the Dojo's server, database, fixture characters and ports separate from the game installation. [Dojo setup](../../../dojo/README.md)

## Library

Search local passages by text and kind. Inspect the source before using a match. Text lookup works with the mind down. An unavailable or incompatible library needs its layout or schema checked. A valid empty search can offer a deeper search. Deeper search needs the verified mind and an already resident embedding model. It does not load a model on demand.

## Lab

Inspect feedback, reward weights, arms, lessons, Turing measures and the weekly history. Check denominators and observation periods. A missing report differs from a measured zero. With control and a running mind, review a proposal to approve, veto or retire it, freeze a family or all learning, review weights, or restore saved learning state. A stale weight revision is refused. Configuration freezes still apply. Thumbs require an eligible generated line and enabled capture. Private and deleted lines keep their access restrictions. Verify the new revision, pending count or freeze state. Unfreezing resumes eligible work. [Lab controls](LAB.md)

## Ops

**Raid debrief** reads the latest completed raid (or current raid before the
first completion) and expands per-boss pulls, responder movements, member
spell hits, deaths and proven checker failures. Separate sections retain
unassigned group-rez reasons/hazards and bound offline stops/outcomes. Missing
evidence is unverified, not success. It refreshes at most once a minute with
a separate four-second budget. Older raids are available with
`python -m leeroy_mind training debrief --raid ID --data-dir <mind-data>`.
See [debrief evidence and limits](../training/README.md#raid-debrief).

**Verification** lists every shipped feature of `verify/features.toml` as failed,
seen working, unverified or switched off, failed first. Expand one for its
switches and each evidence signature: what it means, its count and its first and
last time since the change reached the live install, with short codes. Evidence
comes from bounded newest-first tails of Server.log, Playerbots.log and mind.log
and from mind.db opened read-only; the panel shows how far back each log was read.
A feature with nothing seen is unverified, never working. It refreshes at most
every five minutes with an eight-second budget. The same ledger prints with
`python -m leeroy_mind verify status [--json] --data-dir <mind-data>`.

Open the named **FAIL** and read its check, source, age and reason. Compare Mind's links, the relevant journal or the schedule's last result. Verify the same check on a fresh poll after correcting its cause. Panels for health, night, Realm holds, raids, selfies, backups, release drift and schedules can fail independently. Switches report configuration, dependencies and a restart cue. Confirm effective behavior with a fresh greeting or operation receipt. Cached Git drift, a completed snapshot and a verified off-machine backup answer different questions.

To act, enable `[control] ops_actions = true` and restart mind and console. Controls must be on, the mind verified, owner freshly known offline, and no competing operation or open human raid. An open offline raid blocks actions other than stopping that run. Preview names the target and consequences. Confirm the fresh one-use request. If confirmation times out, inspect the recorded operation before retrying.

- **Release Realm hold** needs an eligible settled unknown movement root, reserved manifest, no children or conflicting evidence, the exact typed root ID and a private reason of 1–200 characters. The console stops the mind cleanly, invokes the release CLI, checks its committed result and attempts restart even after a CLI refusal. Check the release and restart outcomes separately. Native fences can remain. An operator using the CLI directly must stop the mind and confirm the exact ID. [Realm receipts](../realm/README.md)
- **Stop offline raid** needs the original run's terminal and cleanup. **Render** requests the eligible photo booth pass. Inspect its actual outcome, including partial work or preemption. **Snapshot** needs a completed local database snapshot. Labels take 1–40 ASCII letters, digits, hyphens or underscores. The local volume must support hard links; the operation refuses links, junctions and overwrites. Keep the published manual snapshot until the operator chooses to remove it.

Member heroics start disabled. To admit new runs, enable `[life] dungeons = true`, the guarded Discord server (`[server] enabled`, `[guards] enabled`), `Leeroy.MemberDungeon.Enable = 1` and `Leeroy.KeepCast = 1`. Native needs DungeonClear, its eligible dedicated test driver and enabled instance strategies. Verify `member_dungeon_v1` after native reload or reconnect. The mind reads the life switches at startup, session start and nightly reload. Day plans, Godmode and Strings have independent switches. The first rotation uses map 602 with encounter slots 0, 3, 1, 2. Normal heroic entry, loot and lockouts apply.

Ops → Feature switchboard also has collapsible native sections for the installed
`build/bin/RelWithDebInfo/configs/modules/mod_leeroy.conf` and `playerbots.conf`.
They show only catalogued boolean keys, including Leeroy's feature enables and
Playerbots' raid switches. Counts, modes, database strings and unknown keys are
excluded. The source is explicit; tracked `config/` files are never a fallback.
`unset` means the key is absent in that file, not off. `invalid` does not imply
off either: native may use its own default. The first assignment wins as in the
native parser. Missing/unreadable files show unknown values.

These are configured file values, not observed server state. A different build,
alternate config path, environment override or unapplied edit can change what
runs. The panel neither reads those overrides nor controls native. Follow each
feature's reload instructions, then verify its fresh greeting or receipt; apply
restart-only changes at an approved native restart. Reading this panel never
enables day plans, heroics or any other switch. Update `native_switch_keys.py`
from actual `GetOption<bool>` declarations when updating the native/playerbots pin.

Disablement closes proposals and requests cleanup for entered runs. Preserve the original start ID and first terminal. A cleanup-failed result can appear after 60 seconds. At least 600 seconds after the verified failure report, the shared slot can release if native no longer holds a member. A disjoint roster may then run. The original five stay fenced until exact handback is verified, including in a build without DungeonClear. After a crash at the first group-creation intent, the connected leader's independent ownership, group absence, original live position, difficulties, leader flag and matching stored character state can prove handback without any physical restoration. An old character row alone cannot.

For an unresolved heroic, preserve a read-only copy of the characters database's `leeroy_member_dungeon_receipt` row for its realm and case-sensitive original start. Record revision, slot, settled flag, original terminal/time, verified failure time, group/copy, return scope, saved body states and per-GUID handbacks. Compare character/group rows with available live session, master and ownership observations. A verified member can later move. Keep ambiguous members fenced and send the evidence privately to the release owner. The Realm release action has no authority over this receipt. Any manual restoration needs separate authority. [Inspection procedure](../godmode/README.md#inspecting-an-unresolved-return)

## Mind

Check the mind process and OS lock, game servers, game bridge, voice process and voice-to-mind link, model residency, GPU and filtered logs. A listening port, authenticated bridge greeting, known login/logout and healthy voice link are separate observations. Inspect the service identified by the console before using Start, Restart or Stop. Start is offered for stopped/crashed mind, restart for running mind, stop for running/starting mind. A busy lifecycle or ambiguous process ownership can block control. The tab observes authserver and worldserver. Stop/restart asks for confirmation with the owner in game or unknown. Only the checkout’s own mind venv may control its mind. Verify voice by its venv launcher and base interpreter. The lifecycle controller asks for clean stop, retries a busy endpoint and waits up to 60 seconds; a forced stop reports why. Ops Realm release requires a clean stop. After a change, verify the process family, lock and restored links. Distinguish a clean stop, forced stop and crash.

## Installation boundaries

Keep `page.html`, self-hosted fonts, CSS, theme script and ES modules in the console package. The static allowlist and same-origin CSP exclude linked files and inline scripts/styles. Ordinary HTTP requests have 16 KiB header/body limits, a 10-second request/drain deadline and one request per connection. The service caps concurrent connections at 64 and event streams at eight. Uploads have separate bounded media/content-type and same-origin checks. JSON refuses duplicate fields, non-finite numbers and nesting beyond 32 levels. Keep these limits when diagnosing a refused request.

Use the configured data directory for `mind.db`, `mind.toml` and `logs/mind.log`. The console reads these sources; a verified mind owns gameplay writes. The private per-start mind control key, port and PID stay in the owner-only `control.json`; verify the listener PID before contact. The voice mirror uses the voice service’s own socket. The console never reads game database credentials. Serve bounded, identity-checked JPEGs only from approved photo roots. Reject links or junctions anywhere on the path. An absent face uses initials. A selected Dojo layout exposes only bounded reads in its isolated data/runtime roots, never private configs or database contents.

Ordinary browser reads time out after 30 seconds and actions after 120 seconds. An unanswered action may already have completed. Inspect its outcome before another attempt. Stale retained data shows its age. When the page returns to view it resynchronizes from a current snapshot. Ops caches health for up to a minute and budgets each panel separately. A slow source can be skipped while others answer. Private health detail belongs in the operator CLI.

Face retries stop at their next timer when the page is hidden, Hide photos is on, or no
attached face needs that member. Returning and drawing the face starts a fresh request.
Permanent client errors are cached for five minutes; 408 and 429 remain retryable.
Each pending image has one completion listener, so repeated feed redraws do not retain
detached rows. Cache eviction takes one snapshot of attached photo URLs per pass and
never revokes a photo still attached to the page.

## Troubleshooting

| What you see | First check | Evidence of recovery |
| --- | --- | --- |
| Shell opens but reads fail | Reopen the current keyed bookmark. Check the exact Host and Origin and whether the console restarted after rotation. | An authenticated tab refresh succeeds. |
| Phone cannot connect | Check tailnet membership, Serve target, allowed HTTPS hostname and the interactive console task. Keep the listener on loopback. | The keyed phone bookmark opens the same installation as the PC. |
| Last good data, or “Not set up yet” | Check the source age, layout, installed schema and owning feature. Open the tab for a fresh poll. | A newer successful source read or an explicit empty result. |
| Send or control refused | Check medium, owner presence, mind endpoint, feature switch, audience, raid/combat and competing holds. | The permitted operation's own response and resulting state. |
| Guild stock or a member stays held | Match the exact original withdrawal/mail, Realm root, or heroic start. Preserve unknowns and competing evidence. | Confirmed direct job, verified eligible Realm release, or settled heroic return for that start. |
| Health or backup FAIL | Open Ops, identify the failing check and timestamp. Inspect the task's last result, backup manifest and verification time as applicable. | The named check passes on a fresh sample and the expected artifact is verified. |
| Release drift or different About commits | Inspect local checkout and running package identity. Drift uses local cached history. | The intended console restarts and reports the expected local commit/version. |

The page template, static scripts/styles, read-only views, service controls and operator notes live in this package. Feature state and journals live in the configured data directory. Keep generated media, SQLite files, logs, credentials and rehearsal captures out of the public checkout. For deployment, per-tab acceptance and rollback, use the [release checklist](RELEASE.md).
