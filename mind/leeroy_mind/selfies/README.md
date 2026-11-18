# Private photo styling

`[selfies] romance_styling = true` opts new private situational and requested photos into
512 additional clothed outfit templates in `kind.py`, with twelve colour choices. It defaults
to false and is read when the mind starts. An invalid table does not enable it. It needs the
existing selfie/request features enabled; it does not start a worker itself.
The Ops switchboard lists it as "Private photo romance styling".

The same romance stance, relationship stage and world/member romance dial used by talk
select a warm, playful or confident expression. The top two talk bands both use confident,
non-explicit photo styling. Casual outfits become more polished as the level rises, and
going-out outfits gain evening choices. Cold-weather, outdoor and gym clothes keep their
category. Within each category, warm styling favours ordinary outfits, playful styling favours
fitted clothes, and confident styling favours crop tops, mini dresses, off-shoulder tops and
similar date-night choices. These are weighted choices, so each level still has variety.
The lists include fitted tops, crop tops, shorts, mini and midi dresses, date-night
clothes, loungewear and gym clothes. The owner's
saved wardrobe keeps the outfit chosen for the scene; new outfits use her saved colour palette
when present. Requests for work uniforms or current clothes keep those choices. No model call
is added.

The catalogue contains 96 casual, 96 polished, 112 evening, 80 warm, 72 outdoor and 56 gym
templates. These are 512 distinct clothing combinations before colour is applied, not a count
inflated by colour variants. Every template is checked in all twelve colours against the
existing scene screens and word limit. Existing stored scenes keep the outfit they already chose.

Private requests through `[photos] scene_from_words` also allow eligible swimwear photos at
the water using a mirror or timer, as well as a handheld camera. The existing lust style is
still required, the crop remains waist-up, and rear views and indoor or bathing contexts retain
their existing fallbacks. The opted-in request prompt welcomes non-explicit fitted outfits
and appropriate swimwear. The owner's prompt overlay still takes precedence.

Fixed poses add direct eye contact, a slight head tilt, angled shoulders and, for an idle
mirror or timer photo, an over-the-shoulder glance. Action snapshots keep their action;
a requested rear view keeps its existing framing and face-reference scale. Soft daylight,
warm lamplight and golden evening light follow the place, time or requested light source.
Neon, fluorescent and flash requests retain their original light. Poses follow face details
in the prompt; lighting has lower priority so it cannot crowd out her identity.

### Photo preferences and repetition

With styling on, new private choices avoid the outfit templates in her eight newest private
recipes, including planned stock, and avoid the previous expression/pose/light when another
choice is available. Changing only the colour does not evade an outfit repeat. If every
eligible option was recent, the same eligible pool remains available. Work uniforms, current
clothes, saved wardrobe choices and swimwear keep their original outfit selection.

`[learn] apply = true` additionally uses the owner's current thumbs-up/down feedback controls
on photo-bearing lines and thumbs-up/down emoji on the actual DM photo. A feedback-control
vote takes precedence over emoji; opposing thumbs together count as a downvote. Silence, other
members' reactions and ordinary text replies contribute no preference. Likes softly favour
similar clothes, colours, expressions, poses and lighting; the bias is capped at 2x or 0.5x
and stays inside the activity, palette and romance choices already allowed.

The reader considers at most 64 private recipes from the last 90 days, for this member only,
and uses those recorded with romance styling.
Sent photos must have an exact private delivery to the owner. Discord requires the live
message, attachment and chat-line receipt to agree. A removed reaction, changed vote or deleted
message is reflected at the next choice; nothing is copied into a separate preference table.
The Lab/global learning freeze and member freezes stop preference bias immediately. Repeat
avoidance still works. Public requests read no private styling history. Night planning, live
requests and offline rehearsals use the same selector, with no added model call.

This applies only to private photos of eligible fictional adult women, age 25 or older.
Men, non-romantic members, former partners and members with their forward dial at zero
keep their existing choices. Public photos and profile pictures are unchanged, including
manually chosen profile outfits. The photo's real destination wins over the model's audience
choice. The name, likeness, age and content screens, negative prompts and image classifier
threshold are unchanged.

The chosen outfit, expression, `romance_style` and optional `romance_pose`/`romance_light` codes
are stored with the scene, so rendering,
captions and later memories agree. Clothing is required in the image prompt beside her
identity; the expression is optional. The selection is repeatable for a night/member or
request seed, with variation across seeds. Existing photos are not rewritten. Disabling the
setting stops new styling; already planned photos retain their stored recipe and existing
render/send checks. A styled scene is held if its audience becomes public or its age, looks
or romance stance no longer qualifies. No migration, native rebuild or live config edit is needed.

## Selfie guard audit

This audit covers the photo path from a persona or request through rendering, retained stock
and delivery. It does not evaluate a classifier against real images or change the live install.

| Layer | Result |
| --- | --- |
| Model download (`selfie/leeroy_selfie/models.py`) | Keep the `nsfw` model pin, revision and file checks. A pin selects a reproducible download; it is not the acceptance threshold. |
| Configuration and protocol | Keep the configured cutoff and numeric validation. `romance_styling` remains off by default. |
| Persona eligibility (`style.py`, `romance_styling.py`) | Keep age 25+, age-coded appearance exclusions, fictional identity, romance controls and the actual private audience. |
| Outfit and scene composition (`kind.py`, `scene_choices.py`, `words_scene.py`) | Expand the clothed catalogue and fixed expressions, respect the saved wardrobe and palette, and permit private mirror/timer swimwear at water settings. Work uniforms and current clothes remain specific choices. |
| Free-text screens (`kind.py`, `scene.py`, `words_scene.py`) | Retain the age, explicit-content, undressing, real-name, likeness, other-person and prompt-syntax screens. Added clothes and expressions pass the existing screens. |
| Render prompts (`prompt.py`, `scene.py`, `words_scene.py`) | Retain negatives and identity priority. New clothes are required prompt content; expressions remain optional after face details. Disabled/public prompts remain unchanged. |
| Worker and detailer (`selfie/leeroy_selfie/render.py`) | Retain scoring before writing and before/after refinement. Return the full score instead of rounding it before downstream decisions. |
| Stock and delivery (`slot.py`, `photos/request_stock.py`, `photos/send.py`) | Retain score rechecks, current identity/style checks, audience provenance and private delivery. |
| Files and operation | Retain checked image paths, exclusive writes, size/type checks, model hashes, GPU pause, quotas and bounded retries. These protect data and service operation. |

### Score precision and diagnostics

The worker previously accepted a score such as `0.19996` against a `0.2` cutoff but returned
`0.2` after rounding. The mind then rejected the same image, and rounding down could also make
a rejection look inconsistent in diagnostics. Render and caption results now retain the score's
full floating-point precision through JSON. The `selfies try --ask` result and the worker's
standalone detail result likewise report the unrounded score; display-only logs may round it.
An image scoring exactly the cutoff remains rejected.

Boundary tests cover below, equal to and above the cutoff. The fake-model tests demonstrate
consistent decisions and reproducible diagnostics; they do not measure the classifier's real
false-positive rate. The default cutoff, pinned classifier and explicit-content checks are
unchanged. Assessing semantic false positives would require a separately labelled image set;
a score alone is not evidence that an image is explicit or non-explicit.
