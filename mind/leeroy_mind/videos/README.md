# His videos in the guild's Discord

The owner can send a short video (mp4, webm or mov, up to 50 MiB) in a channel or DM of the local
guild server, as he sends a photo (`photos/README.md`, "Uploads"). The member who answers sees a
few stills of it and is told it is a short video she cannot hear.

## Switch

`[videos] enabled = true` in `mind.toml` (default true; an unknown or non-boolean key
reads as off and logs `videos_config_invalid key=...` once). It also needs `[photos] enabled`
and `ffmpeg` and `ffprobe` on the PATH of the console's process (on this PC they come from
Chocolatey, `C:\ProgramData\chocolatey\bin`). Restart the mind and the console after changing
it. The console looks for ffmpeg once at its start: without it the page does not offer videos,
says "Videos need ffmpeg and ffprobe on this PC's PATH." when one is dropped, and logs
`videos_need_tools code=ffmpeg_not_on_path`. Ops lists it as **Discord videos from him**.

A new table rather than a `[photos]` key: older code ignores an unknown table, but reads an
unknown `[photos]` key as an invalid table and turns every photo switch off.

## Attaching

The page's one attach path takes the Attach button, a file dragged from the desktop onto the
open conversation (its feed, header or composer, with a "Drop to attach" overlay) and a pasted
file. One photo or video goes with a message; from several files the first that can be
attached is taken and the page says so. Anything else is refused in words, and so is a video
while videos are off. Text and links dragged in drop as the browser drops them; a file dropped
anywhere else on the page is refused there, so it never opens in place of the console.

The video is not changed in the page. Send uploads it once to `POST /api/discord/video` (raw
body of type `video/mp4`, `video/webm` or `video/quicktime`, same-origin, key, Host and Origin
checked, 50 MiB in up to five minutes), then posts with `video: <token>`. Limits: two uploads a
minute and 50 a local day in the console (rebuilt from the staged facts after a restart), and
in the mind four attachments a minute (shared with photos) and 50 video posts a local day.

## The one read

At upload the console checks the bytes start as their declared type does, then runs `ffprobe`
(a picture stream and the length) and `ffmpeg` once: up to three stills at 10%, 50% and 85% of
the length, one for a clip under two seconds or of unknown length, each fit inside 768 x 512,
side by side 6 black pixels apart in one JPEG on a pipe. Inputs are this PC's own files only
(`-protocol_whitelist file`, the demuxer forced from the declared type: `mov` or `matroska`),
nothing is fetched or sent anywhere, and each run has a deadline (20 s, 60 s). A file it cannot
read is refused ("That video could not be read. Try an mp4.") and stays where it was staged.

## Storage

Nothing here deletes a video, a still or a claim.

- `discord/incoming-video/<token>.<mp4|webm|mov>`, `<token>.jpg` (the stills) and
  `<token>.json` (the facts: type, bytes, length in ms or null, one still's width and height
  with rotation applied, how many stills). `<token>.claimed` marks the token used.
- At the post the mind moves the video to `discord/videos/<message>.<suffix>` and copies the
  stills to `discord/images/<message>.jpg`, the message's ordinary `upload` attachment row
  (what the look sees, and what older code shows as a photo). A name a rolled-back post left
  is kept, and this one takes `<message>-<token>`.
- The facts and the video's path are in meta `discord:video:<message>` (JSON); the day's count
  in `videos:uploads:<date>`. No migration.

## What the member perceives

- The look (`photos/look.py`) runs on the stills with its own instruction: the stills are one
  short video, left to right in time order; describe what happens in it.
- Her answer's instruction: "He sent a short video (12 seconds); it is his video; you saw a few
  stills from it and cannot hear its sound; do not assume he is in it unless he says so. What
  happens in it: ...". A failed look reads "You can't make it out well."
- History lines carry `[video, 12 seconds: ...]`; her memory of it says "sent me a video".
  DM privacy is the photo's.

## The page

`GET /api/discord` gives a video message `video: {ms, w, h, frames, bytes}` beside its `image`.
The feed shows the middle still as a poster with **Play video · 0:12**; Play fetches
`GET /api/discord/video?id=` (key checked, bounded, links refused, the opened file checked)
into a blob and puts a `<video controls>` there (a `<video src>` cannot carry the key). Players
survive the feed's redraws; at most four stay loaded. The console writes any answer in 256 KiB
pieces, each within 10 s, so a phone over the tailnet can take a long video.

## Logs

`discord_video_upload outcome=<code> bytes=<n> ms=<n> frames=<n>` in the console log; the
mind's `discord action=post outcome=<code>` and `discord_look` as for photos. Never a file
name, a token's file contents or what the video shows.

## Trying it

Turn on `[videos] enabled = true` (with `[photos] enabled = true`) and restart the mind and the
console (open console tabs reload themselves into the new page), open a DM in Comms and drag a
short phone video onto the conversation. Check the poster and inline player, and that her
answer engages what happens in it.
