# Private photo styling

`[selfies] romance_styling = true` opts new private situational and requested photos into
80 additional clothed outfit templates in `kind.py`, with twelve colour choices. It defaults
to false and is read when the mind starts. An invalid table does not enable it. It needs the
existing selfie/request features enabled; it does not start a worker itself.

The same romance stance, relationship stage and world/member romance dial used by talk
select a warm, playful or confident expression. The top two talk bands both use confident,
non-explicit photo styling. Casual outfits become more polished as the level rises, and
going-out outfits gain evening choices. Cold-weather, outdoor and gym clothes keep their
category. Requests for work uniforms, current clothes or swimwear retain the existing
resolver. No model call is added.

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
