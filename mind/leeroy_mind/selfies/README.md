# Private photo styling

`[selfies] romance_styling = true` opts new private situational and requested photos into
128 additional clothed outfit templates in `kind.py`, with twelve colour choices. It defaults
to false and is read when the mind starts. An invalid table does not enable it. It needs the
existing selfie/request features enabled; it does not start a worker itself.

The same romance stance, relationship stage and world/member romance dial used by talk
select a warm, playful or confident expression. The top two talk bands both use confident,
non-explicit photo styling. Casual outfits become more polished as the level rises, and
going-out outfits gain evening choices. Cold-weather, outdoor and gym clothes keep their
category. The lists include fitted tops, crop tops, shorts, mini and midi dresses, date-night
clothes, loungewear and gym clothes. Expressions also offer a slight head tilt. The owner's
saved wardrobe keeps the outfit chosen for the scene; new outfits use her saved colour palette
when present. Requests for work uniforms or current clothes keep those choices. No model call
is added.

Private requests through `[photos] scene_from_words` also allow eligible swimwear photos at
the water using a mirror or timer, as well as a handheld camera. The existing lust style is
still required, the crop remains waist-up, and rear views and indoor or bathing contexts retain
their existing fallbacks. The opted-in request prompt welcomes non-explicit fitted outfits
and appropriate swimwear. The owner's prompt overlay still takes precedence.

This applies only to private photos of eligible fictional adult women, age 25 or older.
Men, non-romantic members, former partners and members with their forward dial at zero
keep their existing choices. Public photos and profile pictures are unchanged, including
manually chosen profile outfits. The photo's real destination wins over the model's audience
choice. The name, likeness, age and content screens, negative prompts and image classifier
threshold are unchanged.

The chosen outfit, expression and `romance_style` are stored with the scene, so rendering,
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
