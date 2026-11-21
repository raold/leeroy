# Release the guild console

Use this checklist in an authorized installation window. The owner must be known offline for the release. The later in-game acceptance pass starts after services are healthy. Keep the private log, backup locations and rollback line with the release owner.

For bank decisions, day plans and member heroics, ship dormant first, then use
the [staged rollout](ROLLOUT.md): one key change at a time, real behavior and
return evidence before the next feature, and explicit stop/rollback criteria.

## 1. Establish the release inputs

- [ ] Identify the last installed commit, the public merge commits still needed and the intended checkout, data directory and native build. Verify the expected artifacts and linked native CI for the final source. The historical console dependencies (tasks 15, 16 and 21–27) and the release helper are on public main.
- [ ] For the four-task batch the public order is decisions/bank/strategy/ranks (`c6c6bb81`), day plans (`a6b326be`), member heroics (`32b2d009`), then the public merge commit for this guide and its native recovery correction. Resolve the last SHA from merged main. Include only approved, unapplied commits, in dependency order. Make sure each is already available on the install's cached `origin/main`. The helper performs no fetch.
- [ ] Read the installed mind database's `PRAGMA user_version` in read-only mode and check the ordered migration files under `leeroy_mind/storage/migrations`. The [mind migrator](../storage/db.py) applies the pending versions. Older installations apply every intervening migration in numerical order. For an install at 0061 the next steps are:
  1. [0062](../storage/migrations/0062_decisions_bank.sql) extends decision ownership of Realm direct jobs, bank outflows and strategy evidence.
  2. [0063](../storage/migrations/0063_decision_job_proof.sql) binds actor and expected result to the original command and repairs premature mail references. The guide's audit correction changes no DDL. A later bank reconciliation repairs a historical attachment label only from the exact positive mail receipt.
  3. [0064](../storage/migrations/0064_day_plans.sql) adds day plans, permits, steps and handoffs.
  4. [0065](../storage/migrations/0065_member_heroics.sql) adds member heroic runs, guarded posts and encounter evidence. Native creates its separate characters-database receipt table on the first admitted start. Its absence before then is expected.
- [ ] Keep the feature switches at their approved values. All new heroic admission stays off by default. Deployment and a successful migration do not authorize enabling any feature.

## 2. Preview and run the release

In Git Bash on the selected install, set `GUIDE_MERGE` to the verified public merge SHA. This example assumes none of the four commits is installed. The native flag is required for the heroic recovery adapter as well as the earlier native features.

```sh
bash scripts/ac-release.sh --native --dry-run c6c6bb81 a6b326be 32b2d009 "${GUIDE_MERGE:?set the public guide merge commit}"
```

- [ ] Read every preflight line. Confirm the eligible release branch, clean checkout, no Git operation in progress, no release/build lock conflict, approved paths and no already-applied patch. Confirm positive offline evidence and no open raids. Any `--allow-raid ID` exception requires explicit approval for that exact raid. If a selected change affects voice, the helper also requires voice stopped.
- [ ] Verify there is room for a consistent stopped-service database snapshot and the native binary backup. Check the most recent verified guild backup and its scope. The local mind snapshot does not cover native character state. Preserve outstanding day/heroic and training receipts.
- [ ] Run the identical command without `--dry-run`. Keep its complete output privately. It serializes the release, applies commits in order, stops services for the native release, verifies native stop, snapshots SQLite, saves old binaries, builds, migrates the mind database, starts services and requests a restart of the installed console task. It then runs health and prints the run-specific recovery line.
- [ ] Verify the resulting install HEAD, schema version, service identities, fresh bridge greeting and console restart. Cherry-picking gives the installation its own commit identity. Check About against **that checkout's** HEAD. An absent console task or a task start note needs a separate check even after a successful release. [Release script](../../../scripts/ac-release.sh) and [snapshot and rollback helper](../../../scripts/ac-release-data.py)

## 3. Check the scheduled tasks and phone

In PowerShell, inspect the installed console task before reinstalling it. Read the executable and arguments on its `runs` line.

```powershell
.\scripts\ac-console-schedule.ps1 -Status
```

Copy the installed action's `--root`, `--data-dir` and `--key-file` paths into the placeholders below. Use the key file's path only. Verify its `--allow-host` against the approved MagicDNS hostname. If the task is absent, obtain the approved installation paths and host before continuing. Preview the replacement in the same PowerShell session.

```powershell
$consoleTask = @{
    Root      = '<installed root>'
    DataDir   = '<installed data directory>'
    KeyFile   = '<installed stable key file path>'
    AllowHost = '<approved MagicDNS hostname>'
}
.\scripts\ac-console-schedule.ps1 -Show @consoleTask
```

Compare the preview's executable, working directory and arguments with the approved installation. After they match, replace the task using the same values. Check the resulting `runs` line against the preview. Preserve the existing health and backup schedules and backup configuration.

```powershell
.\scripts\ac-console-schedule.ps1 -Install @consoleTask
.\scripts\ac-console-schedule.ps1 -Status
.\scripts\ac-health-schedule.ps1 -Status
.\scripts\ac-guild-backup-schedule.ps1 -Status
```

- [ ] Verify `leeroy-console` is the owner's Limited interactive logon task, starts the installation's mind venv hidden with the correct host and stable key, and runs a single instance. Check that the task actually starts the new code and that a restart preserves the trusted bookmark. Keep the loopback listener and tailnet-only Tailscale Serve mapping. Test the phone on the tailnet, dark/light themes, scrollable rails, Channels drawer, help, Escape/Close and return focus. Only rotate a key when intended, then restart and validate the replacement and old bookmark. [Console task](../../../scripts/ac-console-schedule.ps1)
- [ ] Check `leeroy-health` runs every 15 minutes while the user is logged on. If absent from an installation intended to use it, install with `scripts/ac-health-schedule.ps1 -Install`. Verify a fresh result and the matching health record. [Health task](../../../scripts/ac-health-schedule.ps1)
- [ ] Verify the daily guild backup task, mounted destination, last run, manifest, verification result and chosen DB coverage. Default is 03:00 local. Preview with `scripts/ac-guild-backup-schedule.ps1 -Show -At '03:00'`; reinstall with `-Install` and the approved configuration, adding `-WithDb` only when native DB coverage is intended. A missed run starts when the user next logs on. Check the backup privately. [Backup task](../../../scripts/ac-guild-backup-schedule.ps1)

## 4. Accept the console with the owner out and in

Start with known logout, then repeat the second column of observations after a fresh owner login. Record the observed time, installation SHA, tab, check and outcome. Keep private text, photos, prompts and keys out of evidence shared publicly. Inspect available controls and receipts. A release alone does not authorize a real transfer, vote, stop, GPU render or force-release.

| Tab | Owner out | Owner in |
| --- | --- | --- |
| Tonight | Read each card's source and date. Confirm known logout and last night or a specific missing reason. | Verify observed login and refreshed kit/signups without exposing private messages. |
| Comms | Discord channels, authorized DMs and groups are readable. WoW send explains the offline gate. Inspect eligible guarded heroic posts and a fresh playing indication if a run exists. | Check the right WoW audience, owner/bridge and reply availability. Check voice room, mute/deafen and receipts for an intended reply. Discord stays separate. |
| Raid | Read recent ledger and any owned offline run. Check current phase and available controls. | Check the actual owner group, leader eligibility and current phase. An intended summon needs its own ACK/job after any handback and a quiet owner group. |
| Guild | Check roster, awards, Decisions and any pending or talk-only state. Inspect exact outflow/mail evidence if present. | Check the current roster and only the eligible poll or overrule. A scheduled rank/strategy restoration remains pending until its own evidence. |
| Social | Read dated relationship/history panels or explicit setup/empty states. | Follow a face into the correct member. Verify history stays tied to its recorded source and date. |
| Personas | Open a profile, Insight, dials and active nudges. Verify stale pins, expiry, disabled edits and applicable photo gates. | Check the profile deep link, scoped Message/Whisper, preview gate and current string eligibility. Any approved action needs its own result. |
| Board | Check forum, delivered letters, write-ups and pending replies. | Check audience, reply/withdraw state and the same stored history after refresh. |
| Calendar | Verify local dates, pending/posted events, signups and missed nights. | Check the owner's signup and a participant profile against the same event. |
| Bank | Check snapshot source/age, purchased or empty tabs, and reconciled withdrawal outflow. | Check refreshed stock and separate money/movement history. A confirmed posting has no mail-header number. |
| Realm | Read rivals, world cards and first-kill rows. For an enabled due day block, check the exact permit and native step evidence. | Observe only a validated overlapping request yielding the activity; verify handback and original command separately. |
| Training | Read the overhead gate, trials, coverage days and eligible cohorts. Keep any unresolved cleanup fenced. | Inspect the played human-raid cohort, applicable strategy and measured evidence. Preserve the distinction between recorded, adopted and effective. |
| Dojo | Check its isolated layout, build/baseline and named gate or explicit absent state. | Verify the same named scenario/replay. The live game login does not start a Dojo job. |
| Library | Check local text search, source and any missing schema. Inspect the deeper-search gate. | Check deeper search only with a resident embedding model and compatible mind. Ordinary text search remains available. |
| Lab | Inspect pending decisions, scores, denominators and freeze state. Verify control or read-only mode. | Check eligible visible feedback and weight version. Private/deleted content stays scoped. |
| Ops | Check each panel, source age, switch dependencies, release drift, schedules and backup. Check fresh preview availability only for an intended operation. | Known login blocks Ops writes. Check the current reason and health separately. Read-only panels remain available. |
| Mind | Verify process family/lock, bridge and game/voice links, positively known absence, filtered logs and resident models. | Confirm a fresh owner session and voice-to-mind link separately. Stop/restart requests must show the in-game confirmation. |

- [ ] On each tab open **?**, check its heading and About version/source, then close it and verify focus returns. Check nested Personas and both header GPU controls. Check that losing a source produces a reason or stale timestamp and that new observations clear it. Use a rehearsal for disruptive mind-down, no-control, missing-schema, timeout and rotation fault cases.
- [ ] For an approved Decisions rollout, verify `[server]` and `[decisions]` enablement after restarting mind and console. Apply the intended roster/mail/restock native keys with `.reload config`, reconnect, and check `member_bank_mail`. Separate private officer posts, one withdrawal, in-flight mail and confirmed posting. A late job repairs only the exact original reference. Inspect strategy/roster restoration at its safe point. [Decision rollout](../decisions/README.md#release-and-switches)
- [ ] For approved day plans, verify both planning and Realm physical switches, a fresh `day_plan_handoff_v1` greeting and the relevant day's schedule. Check arrival and activity separately from planning. Login can coexist with day activity. A validated add, raid or string can yield it. Check deadline, native cleanup and the original request's result. Preserve any dispatched unknown across reboot. [Day plans](../godmode/README.md)
- [ ] For approved heroics, verify both heroic switches, guarded Discord posts, valid DungeonClear driver/strategies and `member_dungeon_v1`. Observe proposal, final-roster consent, the original start, the bound map 602 copy and fresh five-member progress before crediting play. Login alone keeps an accepted run. Confirm encounter facts only for their witnesses; split witnesses can yield separate results without a party-wide speaker. Verify the original native terminal and matching later returned status separately.
- [ ] A shared slot released after a verified cleanup failure and 600 seconds can admit disjoint members. Inspect the original roster's exact handback before reuse. A crash before initial group creation can clear with matching current and persisted origin/difficulty/leader proof for the leader and verified receipt revision. A missing session, independent owner or group, changed state, or unverified readback leaves the hold pending. Preserve the receipt and escalate contradictory evidence; Ops Realm release cannot clear it. [Heroic receipt inspection](../godmode/README.md#inspecting-an-unresolved-return)

## 5. Handle failure or roll back

1. Read the stage and the printed recovery line. A failed dry run or preflight means correct the named prerequisite before retrying.
2. If a failure occurred after applying commits but before service changes, the script attempts to abort the cherry-pick and restore the pre-release checkout. If services were touched, it requires a confirmed stop before restoring the checkout and completed SQLite snapshot. After a native build attempt it also restores the saved binaries. Check the actual result of every step and subsequent restart.
3. A final **post-release health FAIL** returns exit **2** with the completed release still installed. Inspect the individual check with Ops and the [health command](../../../scripts/ac-health.sh), its age, owning service and evidence. Decide with the release owner whether to correct that cause in place or roll back. An exit code alone does not establish which stage failed.
4. For an authorized manual rollback, preserve the failed installation's diagnostics, outstanding native receipts and any data created after the snapshot. Use this run's printed pre-release SHA, snapshot and recovery line. Confirm services stopped before restoring matching files. Omit a snapshot restore if the helper says it was never created. Verify the restored database, prior native build/binaries, restarted services and console task. Recheck health, About, schedules and private phone access. Restoring mind SQLite does not undo native gameplay or settle native member receipts.
5. If stop, restore, build or restart cannot be verified, retain the backup and journals and report the exact failed stage to the release owner. Preserve unresolved body fences for independent handback. Record actual live acceptance separately from rehearsal results.
