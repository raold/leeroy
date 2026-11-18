# Getting around the console

How the page is laid out and addressed since the 2026-11 audit. Everything here is page code
(`static/`) plus four small server pieces: `GET /api/unread`, the `lab` count on
`GET /api/ops/badge`, `last_ts` on each DM of `GET /api/discord`, and the caching and gzip of the
page and its files.

## A member's page

A member's detail is a view of the Personas tab that replaces the list (the wall or table, Choose
selfies and Tuning). Its address is `#personas/<guid>`, a step in the browser's history: the
browser's Back button and the page's "Back to the cast" both return to the list as it was (the same
page of members, the same scroll, the member marked and holding the focus). Leaving it for another
tab is a step forward, so Back from Comms returns to her page. An unknown guid falls back to the
list with a note.

It opens in one click from any face: a face on Raid, Guild, Social, Board, Calendar, Bank and
Tonight is a button to it (`profileButton` in `faces.mjs`; where the face already opens the photo
gallery, as on the Guild roster, the name is the button), and the Comms card keeps its Profile
button. His own face is never a link. The page's first row is Back, then Message (her Discord DM
in Comms, with the message box focused), Whisper (her WoW thread and the reply box), Strings and
Nudge (those panels of the page, their first field focused). The change log is under the member
page and under the list, so a dial set on her page can be undone without going back.

A dial's Preview, and a nudge's, shows under the dial or nudge panel it was asked from, and stays
there through the 30 s redraws until it is closed or another is asked for. Its text lives in the
page's memory only.

## Unread outside the page

While the page is hidden its event stream is closed and no tab polls. One light read,
`GET /api/unread?chat=<newest chat line id held>&discord=<newest Discord message id held>`, at most
every 60 s, counts what came since: whispers to him, messages in his DMs and group DMs, and channel
messages that mention him (`@Name`) or answer one of his. The answer is counts, the newest sender's
name and row ids; never a message's text. The count (what was already unread when the page went
out of sight plus what came since) shows as `(3) Guild Console` in the tab's title with a dot on
its icon, and both clear when the page is looked at again. In sight but without the focus, the
title counts what the page itself knows.

The top bar's Notify switch (off by default, remembered in `guild-console-notify`) asks the
browser for permission on its click only. On, a hidden page raises one notification per newest
message: "<Name> sent a message", never the text.

## Small windows

Up to 1100 px wide the status strip is one line and the tab bar one line, each scrolling sideways,
and the Comms feed's row is as tall as the window has left (`--comms-h`, measured by
`comms/layout.mjs`), so the reply box is in sight at 1024x768 without scrolling.

Under 900 px the page itself scrolls: the status strip goes with it and only the tab bar stays at
the top (the header sticks its strip's height, `--strip-h`, above the window). Comms is the feed
and its reply box; the channel list is a drawer behind the Channels button (which carries the
unread count) that closes on choosing a conversation, on Close, the backdrop or Escape. The Comms
KPI band and the feed's hint are left out there.

## Comms' list

Top to bottom: Conversations, From WoW, Guild Discord, the voice rooms.

Conversations is one list of his Discord DMs and WoW whisper threads: the unread first, then the
newest (`comms/convmodel.mjs`). Every member with a persona has a DM, so a DM nobody has written in
stays out of the list until it gets a message, is open, or he types part of the member's name in
"Find a member" (Enter opens the first match, Escape clears). Group chats keep their own block
under it. The voice rooms are folded until opened (remembered in `guild-console-rooms`), with his
own room said beside the heading.

Keys: `/` focuses the reply box of the open view (unless he is typing), Ctrl+K (Cmd+K) the member
filter, opening the drawer in a small window. The page's first Tab stop is "Skip to the reply box".

Thumbs: with a mouse they wait in a member line's top corner and show when the line is pointed at
or holds the focus, staying once the line is rated; the reasons open as a small panel under them. On
a touch screen they are always under the line, one tap. Nothing asks him to rate.

## Loading

The page and every file under `/static/` answer `Cache-Control: no-cache` with an ETag made of the
console's build id and the file's bytes: a browser revalidates each load and gets a 304 for what
it holds, and a release (another build) is always fetched afresh, so the reload `update.mjs` does
when the event stream's snapshot names another build lands on the new files. Text answers over
1 KB (the page, scripts, the stylesheet, SVG and JSON) go out gzipped to a client that sends
`Accept-Encoding: gzip`; photos, fonts, audio and the event stream are left as they are. API
answers are still `no-store`. The CSP and the keyless static rule are unchanged.

## Times

Every tab writes a time the same way, local and 24-hour: `2026-11-17 09:05` (`static/js/dates.mjs`),
with only the calendar's weekday headings following the browser's locale.
