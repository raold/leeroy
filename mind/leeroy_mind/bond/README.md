# Private relationship activities

`[romance] activities = true` opts in (default false; a non-boolean value disables it).
Nightly moments and moods need no model. Invitations also require `[realm] enabled = true`
and a native rebuild with Realm support, followed by a mind restart. Older native cannot answer the
read-only `realm_visit` probe, so it cannot cause an invitation or move.

Only adult women with fond, drawn or lust stance and nonzero forward dial participate.
Forward and romance dials bound pacing; jealousy has its own global and member caps.
The night records the first *recorded* shared boss kill, one reciprocal late-night game
whisper per pair/night, and a gift actually collected from her Realm mail. The source
receipts and private MemoryWriter row are committed together. Nothing copies mail or chat
text into the moment. Missing-company moods carry no blame; jealousy uses her observed
rivalry state, and playful moods need positive shared evidence. Moods expire after 48 hours.

Bond memories and moods enter only her game whisper with the owner. Public chat, officers,
Discord and other pairs cannot draw them. The callback API accepts `bond_enabled=True`
only when the caller's activities switch and current pair eligibility permit it; ordinary
recall takes the equivalent `include_bond` option. Turning activities off excludes stored
bond memories from live recall as well as stopping the writer and invitations.
Moods appear when the live awareness card is enabled with `[talk] aware = true`.

Implemented activity: **meet at a whitelisted inn and sit**, followed by one gentle private
line through the ordinary talk pipeline and its pacing. Fishing is a later activity task:
native already has `bot_do` "go fishing" through GuildFishing and two whitelisted fishing
places, but this feature does not orchestrate a fishing visit. Duelling, scenic mounted
routes and dungeon runs also remain future activities.

Invitations name an inn in his current zone, at least ten minutes after his login (longer
with quieter forward/romance dials). They use ordinary unprompted-whisper gates: login quiet,
AFK, fight/wipe/debrief, a recent line or open whisper thread, and chattiness zero. One per member and two
total per local calendar day, including declines, failures and silence. One can be pending
at a time; least recently invited members have first choice. A matching game whisper `yes`
accepts; entering within 20 yards of the named inn
after the invitation also accepts. Someone already there is not offered a visit. Native
queries are correlated to the current connection/owner/guild and expire after 35 seconds,
covering three ten-second probe periods. An outstanding probe keeps its correlation;
unsupported native pauses probes until reconnect. Probes do not fill diagnostic receipts;
a zone event or Discord DM cannot accept. No invitations while grouped or held for a raid.
Native rechecks owner/member combat, group and instance state at the move and sit mutations.

An offer expires in ten minutes. An accepted visit lasts at most fifteen. A no/decline,
logout, context change, switch-off or group activity closes it. Higher-priority controllers
retain precedence. Movement uses ActionQueue and BodyLedger; sit uses DirectLedger with a
final invitation/ownership check at transport. Native holds the sitting pose under a bounded
lease and releases it on expiry, disconnect, context/body change, or exact-token stand cleanup.
Restarts
never repeat an invitation, move or emote. Unknown dispatched moves retain their existing
Realm journal holds until native proof or the normal operator release workflow; closing an
invitation does not release an uncertain native operation. No live data or settings change
when this feature is installed with its default switch off.
