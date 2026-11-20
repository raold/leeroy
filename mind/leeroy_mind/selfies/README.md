# Private photo styling

## Worker screening policy

The mind passes the configured `[selfies] nsfw_max` once when starting an image worker.
The setting is optional in TOML; screening is always active. Render and caption callers omit
the cutoff and use that worker policy. Legacy explicit request values remain supported and
validated. Result flags and real scores stay in the response and database, including retries,
delivery checks and meme reuse. Before publishing, the mind still checks the returned score
against its configuration even if the worker reports an image as unflagged. Missing or invalid
result scores are never replaced with zero.
Install the mind and worker changes together; no migration or native rebuild is needed.

## Styling

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
similar date-night choices. Gym clothes are chosen only at the gym (the plain and lust gym
pairings): warming up in the park takes the outdoor wardrobe. These are weighted choices, so
each level still has variety.
The lists include fitted tops, crop tops, shorts, mini and midi dresses, date-night
clothes, loungewear and gym clothes. The owner's
saved wardrobe keeps the outfit chosen for the scene; new outfits use her saved colour palette
when present. Requests for work uniforms or current clothes keep those choices. No model call
is added.

The catalogue contains 96 casual, 96 polished, 112 evening, 80 warm, 72 outdoor and 56 gym
templates. These are 512 distinct clothing combinations before colour is applied, not a count
inflated by colour variants. Every template is checked in all twelve colours against the
existing scene screens and word limit. Existing stored scenes keep the outfit they already chose.
Each is clothed by construction as the photo audit's lists are (`safe.py`, #362): a top and a
bottom, or a dress with its length (`safe.is_complete`); a jacket is worn over a top; at most
`safe.WEAR_MAX_WORDS` words with its article and the longest colour, so the required
`wearing ...` phrase is the outfit itself and never the default garments; and none says what the
audit found drawn wrong (a one-shoulder top, a miniskirt, bike or cycling shorts).

The lust style's place pairings apply to every styled woman, fond and drawn too: no styled
outfit on a bed (a bed, bedroom, pillow or mattress in the place or the moment leaves the scene
unstyled, and a stored styled scene there is held), and gym wear or swimwear beside a bed, a
sofa or the indoors (`scene.INDOOR_RE`) becomes casual.

Private requests through `[photos] scene_from_words` also allow eligible swimwear photos at
the water using a mirror or timer, as well as a handheld camera. The existing lust style is
still required, the crop remains waist-up, and rear views and indoor or bathing contexts retain
their existing fallbacks. The opted-in request prompt welcomes non-explicit fitted outfits
and appropriate swimwear. The owner's prompt overlay still takes precedence. Both are deliberate
widenings (the owner's direction, review of #364): main allows swimwear only in a handheld
selfie, and its request prompt says "Fully clothed, nothing revealing or sexual". Every hard
screen, the negatives and the classifier gate stand behind them unchanged.

Fixed poses add direct eye contact or a slight head tilt; a snapshot or timer photo may also
angle her shoulders and, idle on a timer, glance over one shoulder. A selfie only looks: an arm
holds the camera, so it takes no body pose (`safe.SELFIE_KINDS`, #362). Action snapshots keep their
action; a requested rear view keeps its existing framing and face-reference scale. Soft daylight,
warm lamplight and golden evening light follow the place, time or requested light source.
Neon, fluorescent and flash requests retain their original light. Poses follow face details
in the prompt; lighting has lower priority so it cannot crowd out her identity.

### What the prompt keeps

A styled scene's setting is required (priority 0). Its expression is the first optional phrase
(priority 1, before the activity or moment, after the owner's accessories in a request), and it
never counts in the accessories' reserve, so it costs her no identity word. The outfit is said
whole when that costs nothing; otherwise the prompt says it in a short form
(`romance_styling.short_outfit`: each garment's noun with the word before it, then the nouns
alone), the same garments in the same colour and still complete. `fit_choice` takes the most
detailed form whose required set is the shortest one's and which keeps the expression, then the
activity, wherever a form can (the mind's own count, which never counts below the worker's).
Each prompt carries exactly one `wearing ...` phrase. Expressions are short (no "with a slight
head tilt": the tilt is a pose of its own).

Kept on the worker's real tokenizers (RealVisXL, its fit at 75 tokens), all 512 templates in
the longest colour, expressions spread over the twelve, a 30-year-old fixture woman, "parts" =
bust very large, glutes very large, waist wide, hips very wide:

| Scene | Parts | Setting | Activity | Expression |
| --- | --- | --- | --- | --- |
| snapshot, waist-up, dog walk in the park (the review's fixture) | none | 512 | 208 | 512 |
| same | set | 512 | 0 | 511 |
| selfie, waist-up, coffee at a cafe | none | 512 | 512 | 512 |
| same | set | 512 | 148 | 512 |
| snapshot, three-quarter, walk in the park | none | 512 | 343 | 511 |
| same | set | 512 | 1 | 145 |
| request from his words, handheld selfie in the kitchen | none | 512 | 512 | 512 |
| same | set | 512 | 501 | 512 |

Before (the branch at `55bfb71e`, without #362's poses) the expression was kept in 0 to 57 of
512 and the setting in 52 to 512. The dog walk's activity ("out on a walk with a dog alongside,
the dog looking up at the camera") is the longest; where it and the expression do not both fit,
the expression wins. With the parts set on a three-quarter shot her required set (framing, body,
hair, skin, the pose and the clothes) leaves the expression no room in most cases; nothing of her
identity is traded for it.

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
captions and later memories agree. Clothing and the setting are required in the image prompt
beside her identity; the expression is the first optional phrase (above). The selection is repeatable for a night/member or
request seed, with variation across seeds. Existing photos are not rewritten. Disabling the
setting stops new styling; already planned photos retain their stored recipe and existing
render/send checks. A styled scene is held if its audience becomes public or its age, looks
or romance stance no longer qualifies, or if it stands on a bed. No migration, native rebuild or
live config edit is needed.

**Rollback hazard.** Remove `romance_styling` from `[selfies]` in `mind.toml` before rolling back
to code older than this feature: older code treats it as an unknown key, which switches
`variety`, `varied_sets`, `critic`, `best_of`, `param_bandit` and the detailer off. Styled scenes
already planned keep their stored outfit; older code ignores `romance_style` and renders them as
plain scenes, without the styling holds above.

## Selfie guard audit

This audit covers the photo path from a persona or request through rendering, retained stock
and delivery. It does not evaluate a classifier against real images or change the live install.

| Layer | Result |
| --- | --- |
| Model download (`selfie/leeroy_selfie/models.py`) | Keep the `nsfw` model pin, revision and file checks. A pin selects a reproducible download; it is not the acceptance threshold. |
| Configuration and protocol | Keep the configured cutoff and numeric validation. `romance_styling` remains off by default. |
| Persona eligibility (`style.py`, `romance_styling.py`) | Keep age 25+, age-coded appearance exclusions, fictional identity, romance controls and the actual private audience. |
| Outfit and scene composition (`kind.py`, `scene_choices.py`, `words_scene.py`) | Expand the clothed catalogue (every template complete by `safe.is_complete`, none of the audit's triggers) and fixed expressions, respect the saved wardrobe and palette, and permit private mirror/timer swimwear at water settings. The indoor and bed pairings and the gym pairing apply to every styled woman. Work uniforms and current clothes remain specific choices. |
| Free-text screens (`kind.py`, `scene.py`, `words_scene.py`) | Retain the age, explicit-content, undressing, real-name, likeness, other-person and prompt-syntax screens. Added clothes and expressions pass the existing screens. |
| Render prompts (`prompt.py`, `scene.py`, `words_scene.py`) | Retain negatives and identity priority. One required `wearing ...` phrase through #362's clothing path (`safe.wearing`, `fit_clothed`) and the required setting; the expression is the first optional phrase and the outfit may take a short form to make its room. Disabled/public prompts remain unchanged. |
| Worker and detailer (`selfie/leeroy_selfie/`) | Unchanged: scoring before writing and before/after refinement, the score rounded as on main. |
| Stock and delivery (`slot.py`, `photos/request_stock.py`, `photos/send.py`) | Retain score rechecks, current identity/style checks, audience provenance and private delivery. |
| Files and operation | Retain checked image paths, exclusive writes, size/type checks, model hashes, GPU pause, quotas and bounded retries. These protect data and service operation. |

## Local FLUX LoRA

The mind can select the worker's local adapter in `[selfies]`:

```toml
backend = "flux1-dev"
lora = "F:/path/to/selfie-data/models/loras/portrait.safetensors"
lora_scale = 1.0
```

The adapter is off unless `lora` names an absolute local `.safetensors` path.
Strength defaults to 1.0 and accepts 0 through 2. Install the worker's optional
`lora` extra first. Restart the mind after changing these settings; every rendering
worker, including identity workers, then receives the same adapter and strength.
Embedding workers do not load it. The worker validates and hashes the file before
loading models and reports its hash and strength in readiness metadata.

To return to the base model, remove both `lora` and `lora_scale` and restart the
mind. Setting strength to 0 still loads the adapter. Existing image screening,
identity conditioning and render scheduling are unchanged.
