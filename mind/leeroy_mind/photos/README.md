# Photos in the guild's Discord

The owner can attach a photo to a post in the local guild server (a channel or a DM), and the
members who answer look at it first.

## Switches

`[photos]` in `mind.toml`; all three switches default false, and an invalid table means off.

- `enabled = true` allows owner uploads. Restart both the mind and the console after
  changing it.
- `send = true` lets members attach their retained, screened photos to their Discord messages.
  It requires `[selfies] enabled = true`, `[selfies] snaps = true`, and `[server] enabled = true`.
  A missing prerequisite logs `photos_send_needs key=...` once at startup and leaves delivery off.
  It is independent of owner uploads: `enabled` may remain false with `send` true.
- `requests = true` lets a member make a new selfie when the owner asks in Discord.
  It requires `send` and all its prerequisites. Restart the mind after changing it.
  With requests off, no request recognizer, queue, or GPU borrow gate is attached.

## Requested photos

Ask in the member's DM, or name/reply to the member in a guild channel: for example,
"send me a selfie" or "pic?". An unnamed channel request and a question about a photo
already sent keep the ordinary answer path. Group DMs are outside this feature.

When the bridge confirms the owner is out of WoW, the member chooses a scene using the
existing allowed choices and makes the photo before answering. Comms shows her composing
while she prepares it. The request's words provide context to the live model; they never
become image-prompt text. A plain member answers in the channel where asked. A lust-style
member sends her photo privately and says so in her channel answer only after the private
delivery commits. Adult age 25+, clothing, name, NSFW and caption checks still apply.

The render temporarily borrows the GPU despite the server/Discord presence leases. All
mind chat clients wait, including background extraction; embeddings remain available.
Existing chat calls finish before any model unload. The work budget is 180 seconds for
drain, unload, worker startup and rendering. Worker shutdown and live-model reload must
finish safely even after that timeout. Waiting turns receive deadline credit for the full
admission wait, including cleanup, so that wait alone cannot expire an answer. A login,
hard GPU lease, Free GPU pause or shutdown interrupts the render. Free GPU pause wins and
does not reload the live model until use resumes.

While the owner is in WoW, presence is unknown, or borrowing is unavailable, the member
uses an audience-fit retained photo if available: a ready snap first, then her gallery.
The retained image is copied; its original is never moved or consumed. Its caption describes
that retained photo, not the new scene being planned. The new photo waits for ten continuous
minutes of confirmed offline presence and no running night. Unrendered requests expire
after 24 hours; a rendered photo survives restart until its delivery receipt commits.

Requested photos have separate limits: one request per member per ten minutes, six admitted
borrows per hour and thirty per local date across the guild. They skip and do not spend the
spontaneous photo draw, two-day gap or weekly/daily ration. The selfies dial at zero still
means no photo. A retained image is not repeated to the same audience within thirty days,
by a request or by a spontaneous photo: a snap copied for a request stays ready, but no
opener, answer or channel seed offers it to that audience again in that time.
The attachment, chat mirror, snap and request delivery receipts commit together. A failed
render gets one ordinary answer in the member's words. No retained photo file is deleted.

A dropped answer is retried for the asked member only; other members named in the same post
answer it once. Her first answer, the fresh photo's follow-up and a failure notice each get
their first try and at most two retries (about three minutes apart for the first answer, one
minute for the others). After that the request ends (`passed`, failure `retry_limit` unless a
failure was already recorded) and its unsent photos are closed; their files stay.

`selfie_request` logs ids, channel kinds and outcome codes. `selfie_request_render` reports
total, unload, startup, render, stop and reload timings, without words or prompts. Rehearsal
timings use fake workers and model administration. For release, measure these phases on
the actual machine, confirm the worker tree is gone before live preload, verify chat resumes,
and inspect the live model's GPU residency after handback.

## Members' photos

- A member can attach a private `him` snap to a DM opener or an answer to his own DM. He need
  not be online. When replies on her own time are enabled, the photo is offered only when
  that deferred reply actually speaks.
- Public `anyone` snaps start conversations in `food`, `pets`, `irl`, or `pics`, according to
  the scene. The member pictured opens the conversation; other members see a short scene
  note beside the caption. A private snap never enters a channel. Lust-style snaps only go
  to that member's DM with him; nightly channel stock excludes members whose scene recipe
  is the lust style.
- Her DM photo is remembered in their whispers too, with its original Discord DM label.
  Its scene is available only to their pair, including the awareness card. Deleted messages
  hide their notes and images while retaining the files.
- The selfies world dial keeps its existing private weekly ration. Channel photos have a
  separate local-day budget: `0, 0.5, 1, 2, 3, 4, 5, 6, 8, 10, 12`. The half-photo slot uses
  a stable draw per local date, so repeated checks and restarts cannot grant extra chances.
  Both counters survive restart, and each member still waits two days between photos.
- The night stocks both outlets, private first, within the existing member stock, scene
  and render caps. Birthday and life-beat priority is unchanged. With delivery off, planning
  follows the previous path.
- Only one photo may be reserved at a time. Its caption, Discord attachment, chat line,
  sent receipt and budget spend commit together. A guard rejection, unsafe caption, failed
  delivery or cancelled turn releases the reservation without spending the photo ration.
  Pick, looks, phase, expiry, current NSFW threshold, file and audience checks repeat before
  delivery. No photo file is deleted.
- An answer owed to his DM (on time or on her own time) never dies with its photo: when its
  caption fails the screens or the photo goes stale before it is stored, the photo is released
  (or expired, when stale) and the answer is asked once more without it. An opener is
  dropped with its photo (`photo_words`, `photo_stale`). A channel photo seed is spent only
  once its photo was offered, and a member inside her two-day gap offers no seed.
- Discord delivery and its new outlet stock require adults 25 and over, clothed and
  non-explicit. This delivery floor is stricter than the existing plain profile renderer's
  schema age floor; profile rendering and the delivery-off planning path keep their previous
  behavior. Captions also pass the existing explicit-content, clothing and age screens.

## Uploads

- Attach or paste an image in a Discord channel or DM. The page resizes it to a JPEG and
  strips its metadata locally before upload. Send stays disabled while a photo is being
  prepared.
- Limits: 4 MiB a photo, four a minute and 200 a local day. After a console restart, the
  day's count is rebuilt from the staged files. Images refused before they are written
  do not count.
- `POST /api/discord/upload` (raw `image/jpeg`, same-origin, key, Host and Origin checked)
  stages the file under `discord/incoming` and returns a token. The post names that token,
  and the mind adopts the file into `discord/images` when it stores the message. No image
  bytes cross the control endpoint.
- Files under `discord/incoming` and `discord/images` are kept, including unposted uploads
  and the photos of deleted messages. Nothing here deletes a photo.
- Owner uploads have no NSFW scan. Generated selfies keep their own gates.

## The look

- After the post is stored, one bounded vision call on the live model (30 s) describes the
  photo. The description is screened: sentences naming the owner or a member are dropped,
  and the result is capped at 600 characters.
- Answers use that description. The instruction always tells members it is his photo and
  not to assume he is in it.
- A member who answers a photo post remembers it as an observation. A DM's memory stays
  private to that member.

## Storage

Migration `0038_discord_photos.sql` supplies `discord_attachment` and the `dm` and
`discord` snap outlets. Migration `0044_selfie_requests.sql` adds request-owned snaps,
destinations, delivery and borrow receipts. The console serves a
photo with `GET /api/discord/image?id=&size=sm|lg`; a snap's small size uses its thumbnail.

After release, post a photo of something known and check that the answer engages what it
shows. With the delivery switches on and stock available, check a member's DM photo and a
channel photo conversation. These switches are operator choices; tests use only fake models,
rendered fixtures and isolated ports.
