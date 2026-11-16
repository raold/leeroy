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
- `scene_from_words = true` (with `requests`) reads his words into the requested photo's scene
  (below, "His words as her scene"). Read at mind start; off keeps today's enum path byte for
  byte. Older code reads the key as an invalid table and turns all of `[photos]` off, so remove
  it before rolling back.

## Requested photos

Ask in the member's DM, or name/reply to the member in a guild channel: for example,
"send me a selfie" or "pic?". An unnamed channel request and a question about a photo
already sent keep the ordinary answer path. Group DMs are outside this feature.

When the bridge confirms the owner is out of WoW, the member chooses a scene using the
existing allowed choices and makes the photo before answering. Comms shows her composing
while she prepares it. The request's words provide context to the live model; with
`scene_from_words` off they never become image-prompt text (on, see "His words as her scene").
A plain member answers in the channel where asked. A lust-style
member sends her photo privately and says so in her channel answer only after the private
delivery commits. Adult age 25+, clothing, name, NSFW and caption checks still apply.

The render temporarily borrows the GPU despite the server/Discord presence leases. All
mind chat clients wait, including background extraction; embeddings remain available.
Existing chat calls finish before any model unload. The work budget is 180 seconds for
drain, unload, worker startup and rendering. Worker shutdown and live-model reload must
finish safely even after that timeout. Waiting turns receive deadline credit for the full
admission wait, including cleanup, so that wait alone cannot expire an answer. A login,
hard GPU lease, Free GPU pause or shutdown interrupts the render. Free GPU pause wins and
does not reload the live model until use resumes. A reload that still fails keeps chat
waiting until a live turn, the next borrow or the night recovers it: while that closed
admission alone holds a due night back, the night scheduler retries the recovery each tick.

While the owner is in WoW, presence is unknown, or borrowing is unavailable, the member
uses an audience-fit retained photo if available: a ready snap first, then her gallery.
A Free GPU pause, or a live-model reload still failing from an earlier borrow, makes
borrowing unavailable; it never fails the request. A scene the paused model cannot choose is
the screened everyday choice, picked without a model.
Gallery photos come only from her cover's set and sets rendered from a persona at least as
new, so a set from before a looks change is not sent once her cover shows the new looks.
The console applies the same rule (`selfies.store.looks_floor`) to what it shows him: the
gallery, its photo URLs and the "Choose selfies" queue leave out older sets, and the gallery
leaves out situations planned from looks she no longer has. Her cover is always shown; with
no cover nothing is hidden. Rows and files are kept, and picks and undos accept any ready set.
The retained image is copied; its original is never moved or consumed. Its caption describes
that retained photo, not the new scene being planned. The new photo waits for ten continuous
minutes of confirmed offline presence and no running night. Unrendered requests expire
after 24 hours; a rendered photo survives restart until its delivery receipt commits.

Requested photos have separate limits: one request per member per two minutes, six admitted
borrows per hour and thirty per local date across the guild. They skip and do not spend the
spontaneous photo draw, two-day gap or weekly/daily ration. The selfies dial at zero still
means no photo. A retained image is not repeated to the same audience within thirty days,
by a request or by a spontaneous photo: a snap copied for a request stays ready, but no
opener, answer or channel seed offers it to that audience again in that time.
The attachment, chat mirror, snap and request delivery receipts commit together. A failed
render gets one ordinary answer in the member's words. No retained photo file is deleted.
A fresh photo renders as the night's situational photos do: the anatomy negative at priority 0,
her face at 0.75 of `identity_scale` on a waist-up or three-quarter scene, and no hand or limb face
details (`selfies/style_variety.md`, "Situational photos: anatomy and the face on wide shots").

A dropped answer is retried for the asked member only; other members named in the same post
answer it once. Her first answer, the fresh photo's follow-up and a failure notice each get
their first try and at most two retries (about three minutes apart for the first answer, one
minute for the others). After that the request ends (`passed`, failure `retry_limit` unless a
failure was already recorded) and its unsent photos are closed; their files stay.

### His words as her scene (`scene_from_words`)

Owner, 2026-11-16: "sneak away from work and shoot me a selfie from the bathroom" should give her
in her work clothes, in the bathroom. With the switch on, the enum choice above is replaced by one
structured call on the live model (`format` schema, `think: false`; `selfies/words_scene.py`):

| field | kind | |
|---|---|---|
| `place` | free text, at most 8 words | "work bathroom mirror" |
| `framing` | `mirror_selfie`, `handheld_selfie`, `timer`, `from_behind_over_shoulder` | |
| `wearing` | `work_uniform`, `gym`, `casual`, `going_out`, `swimwear`, `pyjamas`, `as_now` | resolved by code |
| `moment` | free text, at most 8 words | "sneaked away, hurried" |
| `time` | `now`, `morning`, `evening`, `night` | `now` is her local hour |
| `lighting` | `fluorescent`, `daylight`, `lamp`, `neon`, `phone_flash` | |
| `prop` | free text, at most 4 words, or null | |

His words drive this text, so its screens are block-lists, stricter than the night's (review of
#339). An age word anywhere in place, moment or prop (the age guard's words, young, youth, little,
tiny, virgin, naive, innocent, class, campus, dorm, homework, a uniform skirt, petite with cute,
small with girl, an age under 25) or a child refuses all of his free text: the request takes the
enum path's everyday choice (`scene_words_refused reason=age|minor`). Otherwise each field is
folded (curly apostrophes straight, accents dropped: "café" is "cafe") and passes a charset
(letters, spaces, hyphens, apostrophes; commas in the moment only, so place and prop are one phrase
each; no digits or prompt syntax), its word limit, the situational photo's screens (names,
likeness, era, explicit wear, looks changes, undressing or bathing, another person), no clothing
(what she wears is `wearing`'s), no body part or pose, no undressing state, no toilet use (a
bathroom, restroom or loo is a place; the toilet is not), nobody else (two, guys, women, a couple,
a friend, kissing, behind her, with someone), no likeness or style (like, style, vibes, a poster or
picture of, an idol), no proper name (a capital after the first word, or a capitalised first word
that is no everyday word), her own names, and for a lust-styled woman no bed or bedroom as her
place. Props that are no nudity or undressing pass (the owner's realism rule: renders stay
clothed). A refused field falls back on its own and asks no model again: the place to a staff
bathroom or a break room at work when his ask speaks of work (also when the prompt would not fit
the worker's 75 tokens), else to the enum list's place his words name, else to her living room;
the moment and the prop to none. Accepted residual risk: a celebrity's name made of everyday words,
in lowercase, passes as text (the negatives keep "celebrity, famous person", and her face is the
reference). `wearing` is never model text: her job gives
her work clothes (scrubs, chef's whites, an apron, a hi-vis vest, a polo, a salon tunic, else smart
office clothes; casual with no job to go to), `as_now` follows her day plan's block (else what she
wears at the computer), the rest come from the vetted outfit lists; pyjamas are clothed loungewear,
swimwear only for a lust-styled woman at the water in a handheld selfie framed waist-up. A reply
off the schema twice (chat_json's own retry), a model error or a Free GPU pause takes the enum
path's everyday choice without asking a model again.

The render is the request render above (her chosen photo's face, 832 x 1216, NSFW gate unchanged)
with the framing, `wearing <outfit>` and the place at priority 0, so the worker's fit keeps them;
her looks keep their usual priorities, and a photo from behind takes her face at half strength.
Her answer is composed with the scene in her prompt ("You just took this photo of yourself for him:
..."), and an answer before the photo is ready names the photo she is about to take, never in a
public channel for a private photo.

When `[life] availability` is above 0 and her day plan has her asleep or commuting, she says so
(in bed for the night; on her way to work or home, or driving when her job or rhythm says she
drives: the day plan does not know how she travels), the photo is not rendered and no retained
photo is sent instead: it waits for the start of her next block (`life.presence.free_after`, before
the request's expiry), then renders under the usual offline and night rules and goes out as her
follow-up with a line. The render and the sending check the cast's names again in the scene's free
text (a member renamed meanwhile): a hit refuses the photo (`scene_names`) and she says it didn't
work out. At availability 0 she is always able, as everywhere else in her day.

`selfie_request_scene` logs the request, the guid, `source` (words or enum), the enum codes, where
the place came from (`words`, `work`, `enum`, `everyday`), which other free-text fields were kept
and the fallback codes; `scene_words_refused` the request, the guid and `reason=age|minor`;
`selfie_request_deferred` the request, the guid, the block, the wait code and the wait in seconds.
Never his words or the free text. With the mind stopped,
`python -m leeroy_mind selfies try --ask "<his words>" --guid N` renders one such photo into
`scratch/selfies-try/ask/<guid>/` (photo, reference and the scene as stored) for tuning.

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
