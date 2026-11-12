# Photos in the guild's Discord

The owner can attach a photo to a post in the local guild server (a channel or a DM), and the
members who answer look at it first.

## Switches

`[photos]` in `mind.toml`; both default false, and an invalid table means off.

- `enabled = true` allows owner uploads. Restart both the mind and the console after
  changing it.
- `send` is reserved for the next photo phase (members' own photos) and sends nothing yet.

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

Migration `00NN_discord_photos.sql` rebuilds `discord_message` (adding the `bot` origin
reserved for later server bots) and adds `discord_attachment` (`upload`, `snap`, and `meme`
reserved for later). The console serves a photo with `GET /api/discord/image?id=&size=sm|lg`.
