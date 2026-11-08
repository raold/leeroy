"""Across-day near duplicates, shared evening phrases, and stock openings/endings.

Safe on a live read connection (pure, no SQL, no writes): `ngrams`, `shingle_overlap`,
`pair_score`, `best_repeat`, `phrase_keys`, `shared_by_others`, `stock_keys`.
SCRATCH ONLY: `detect`, which builds temp tables and a temp vec0 index on its
connection and so requires a store marked by `common.mark_scratch`.
"""
from collections import defaultdict

from .common import Flag, day, evening, require_scratch, similarity, words

NGRAM_SIZE = 3
MIN_WORDS = 4
NGRAM_OVERLAP = 0.70
EMBEDDING_COSINE = 0.94
NEIGHBOURS = 16
GUILD_PHRASE_WORDS = 5
GUILD_MEMBERS = 3
STOCK_WORDS = 3
STOCK_COUNT = 3
STOCK_SHARE = 0.30

# Ordinary conversational phrases that many members say without copying one another.
# A guild phrase touching a word of one of these is ignored.
# Contractions tokenise on the apostrophe, so "don't know" is "don t know".
STOP_PHRASES = tuple(words(text) for text in (
    'what do you think', 'what do you guys think', 'what do you want to', 'what are you doing',
    'what are you up to', 'how are you doing', 'how is it going', 'how was your day',
    'do you want to', 'do you guys want to', 'does anyone want to', 'anyone want to',
    'would you like to', 'are you ready', 'are you guys ready', 'is anyone else',
    'let me know', 'let me see', 'let us go', 'i do not know', "i don't know", 'i have no idea',
    'i am not sure', 'i think we should', 'i think so', 'i would like to', 'i just want to',
    'thank you so much', 'thanks for the', 'good to see you', 'nice to meet you', 'see you all',
    'see you later', 'good luck to', 'looking forward to', 'by the way', 'a lot of',
    'it looks like', 'it was a', 'going to be', 'we should probably', 'is going to',
    'you want to', 'i was just', 'at the same time', 'as soon as', 'one of the',
))


def ngrams(tokens, size=NGRAM_SIZE):
    return {tokens[i:i + size] for i in range(len(tokens) - size + 1)}


def shingle_overlap(a, b):
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def pair_score(a_shingles, b_shingles, cosine_score=0.0):
    """Score two lines: (n-gram overlap, best of overlap and cosine, is near duplicate)."""
    overlap = shingle_overlap(a_shingles, b_shingles)
    return overlap, max(overlap, cosine_score), overlap >= NGRAM_OVERLAP or cosine_score >= EMBEDDING_COSINE


def best_repeat(text, vector, recent):
    """One draft against a member's recent lines `[(id, text, vector or None)]`.

    Returns (line_id, score) of the strongest near duplicate, or None. Pure.
    """
    tokens = words(text)
    if len(tokens) < MIN_WORDS:
        return None
    mine, best = ngrams(tokens), None
    for other_id, other_text, other_vector in recent:
        cosine_score = similarity(vector, other_vector) if vector is not None and other_vector is not None else 0.0
        _, score, hit = pair_score(mine, ngrams(words(other_text)), cosine_score)
        if hit and (best is None or score > best[1]):
            best = (other_id, round(score, 6))
    return best


def _stop_covered(tokens):
    """Word positions inside any stop phrase."""
    covered = set()
    for stop in STOP_PHRASES:
        for i in range(len(tokens) - len(stop) + 1):
            if tokens[i:i + len(stop)] == stop:
                covered.update(range(i, i + len(stop)))
    return covered


def phrase_keys(tokens):
    """The phrases of one line that can count as shared: long enough, and containing
    no word that belongs to a stop phrase (so "what do you think about the plan" has none).
    """
    covered = _stop_covered(tokens)
    return {tuple(tokens[i:i + GUILD_PHRASE_WORDS]) for i in range(len(tokens) - GUILD_PHRASE_WORDS + 1)
            if covered.isdisjoint(range(i, i + GUILD_PHRASE_WORDS))}


def shared_by_others(tokens, guid, members_by_phrase):
    """Phrases of this line that at least GUILD_MEMBERS - 1 other members already used.

    `members_by_phrase` maps phrase to the set of member guids who said it that evening.
    """
    return {phrase for phrase in phrase_keys(tokens)
            if len(members_by_phrase.get(phrase, set()) - {guid}) >= GUILD_MEMBERS - 1}


def stock_keys(tokens):
    if len(tokens) < MIN_WORDS:
        return ()
    return (('stock_opening', tokens[:STOCK_WORDS]), ('stock_closing', tokens[-STOCK_WORDS:]))


def detect(store, lines, vectors):
    """SCRATCH ONLY: creates temp tables on `store.conn`; raises on an unmarked store."""
    require_scratch(store)
    conn = store.conn
    flags, checked = [], 0
    by_id = {line.id: line for line in lines}
    tokens = {line.id: words(line.text) for line in lines}
    shingles = {key: ngrams(value) for key, value in tokens.items()}
    # The index dimension comes from the embedder's own vectors (float32 bytes).
    dims = {len(vec) // 4 for vec in vectors.values()}
    if len(dims) > 1:
        raise ValueError('mixed_embedding_dimensions')
    use_index = bool(dims)
    # Temp tables live only on the scratch connection. Filter dates before KNN:
    # same-evening chatter must not crowd out earlier-day candidates.
    conn.execute('CREATE TEMP TABLE talk_repeat_day(id INTEGER PRIMARY KEY, day TEXT)')
    if use_index:
        conn.execute('CREATE VIRTUAL TABLE temp.talk_repeat_vec USING vec0('
                     'line_id INTEGER PRIMARY KEY, guid INTEGER PARTITION KEY, '
                     f'embedding float[{dims.pop()}] distance_metric=cosine, chunk_size=16)')
    lexical = defaultdict(set)
    try:
        for line in sorted(lines, key=lambda item: (item.ts, item.id)):
            if len(tokens[line.id]) < MIN_WORDS:
                continue
            candidates = set()
            for shingle in shingles[line.id]:
                candidates.update(lexical[line.guid, shingle])
            similarities = {}
            if line.id in vectors:
                rows = conn.execute(
                    'SELECT line_id,distance FROM talk_repeat_vec WHERE embedding MATCH ? AND k=? '
                    'AND guid=? AND line_id IN (SELECT id FROM talk_repeat_day WHERE day<>?) '
                    'ORDER BY distance', (vectors[line.id], NEIGHBOURS, line.guid, day(line.ts)))
                similarities = {row[0]: 1 - row[1] for row in rows}
                candidates.update(similarities)
                checked += 1
            for other_id in sorted(candidates):
                other = by_id[other_id]
                if day(other.ts) == day(line.ts):
                    continue
                _, best, hit = pair_score(shingles[line.id], shingles[other_id], similarities.get(other_id, 0))
                if hit:
                    flags.append(Flag(line.id, line.guid, 'member_across_days', other_id, round(best, 6)))
                    break
            for shingle in shingles[line.id]:
                lexical[line.guid, shingle].add(line.id)
            if line.id in vectors:
                conn.execute('INSERT INTO talk_repeat_day VALUES (?,?)', (line.id, day(line.ts)))
                conn.execute('INSERT INTO talk_repeat_vec VALUES (?,?,?)', (line.id, line.guid, vectors[line.id]))
    finally:
        if use_index:
            conn.execute('DROP TABLE temp.talk_repeat_vec')
        conn.execute('DROP TABLE temp.talk_repeat_day')
    phrases, stocks, member_counts = defaultdict(list), defaultdict(list), defaultdict(int)
    for line in lines:
        own = tokens[line.id]
        member_counts[line.guid] += 1
        bucket = evening(line.ts)
        if bucket is not None:
            for phrase in phrase_keys(own):
                phrases[bucket, phrase].append(line)
        for rule, key in stock_keys(own):
            stocks[line.guid, rule, key].append(line)
    shared = set()
    for group in phrases.values():
        if len({line.guid for line in group}) >= GUILD_MEMBERS:
            shared.update(line.id for line in group)
    flags.extend(Flag(key, by_id[key].guid, 'guild_evening_phrase') for key in sorted(shared))
    for (guid, rule, _private_phrase), group in stocks.items():
        if len(group) >= STOCK_COUNT and len(group) / member_counts[guid] >= STOCK_SHARE:
            flags.extend(Flag(line.id, guid, rule) for line in group)
    return flags, dict(embedding_checked=checked, neighbours=NEIGHBOURS,
                       min_words=MIN_WORDS, ngram_overlap=NGRAM_OVERLAP, embedding_cosine=EMBEDDING_COSINE,
                       guild_phrase_words=GUILD_PHRASE_WORDS, guild_members=GUILD_MEMBERS,
                       stock_words=STOCK_WORDS, stock_count=STOCK_COUNT, stock_share=STOCK_SHARE,
                       stop_phrases=len(STOP_PHRASES))
