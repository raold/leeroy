# Photos in the guild's Discord

The owner can attach a photo to a post in the local guild server (a channel or a DM), and the
members who answer look at it first.

## Switches

`[photos]` in `mind.toml`; both default false, and an invalid table means off.

- `enabled = true` allows owner uploads. Restart both the mind and the console after
  changing it.
- `send = true` lets members attach their retained, screened photos to their Discord messages.
  It requires `[selfies] enabled = true`, `[selfies] snaps = true`, and `[server] enabled = true`.
  A missing prerequisite logs `photos_send_needs key=...` once at startup and leaves delivery off.
  It is independent of owner uploads: `enabled` may remain false with `send` true.

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

Migration `0038_discord_photos.sql` already supplies `discord_attachment` and the `dm` and
`discord` snap outlets. Member delivery needs no additional migration. The console serves a
photo with `GET /api/discord/image?id=&size=sm|lg`; a snap's small size uses its thumbnail.

After release, post a photo of something known and check that the answer engages what it
shows. With the delivery switches on and stock available, check a member's DM photo and a
channel photo conversation. These switches are operator choices; tests use only fake models,
rendered fixtures and isolated ports.
