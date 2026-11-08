"""Across-day near duplicates, shared evening phrases, and stock openings/endings."""
from collections import defaultdict

from .common import Flag, day, evening, words

NGRAM_SIZE = 3
MIN_WORDS = 4
NGRAM_OVERLAP = 0.70
EMBEDDING_COSINE = 0.94
NEIGHBOURS = 16
GUILD_PHRASE_WORDS = 4
GUILD_MEMBERS = 3
STOCK_WORDS = 3
STOCK_COUNT = 3
STOCK_SHARE = 0.30


def ngrams(tokens, size=NGRAM_SIZE):
    return {tokens[i:i + size] for i in range(len(tokens) - size + 1)}


def detect(conn, lines, vectors):
    flags, checked = [], 0
    by_id = {line.id: line for line in lines}
    tokens = {line.id: words(line.text) for line in lines}
    shingles = {key: ngrams(value) for key, value in tokens.items()}
    # Temp tables live only on the scratch connection. Filter dates before KNN:
    # same-evening chatter must not crowd out earlier-day candidates.
    conn.execute('CREATE TEMP TABLE talk_repeat_day(id INTEGER PRIMARY KEY, day TEXT)')
    conn.execute('CREATE VIRTUAL TABLE temp.talk_repeat_vec USING vec0('
                 'line_id INTEGER PRIMARY KEY, guid INTEGER PARTITION KEY, '
                 'embedding float[512] distance_metric=cosine, chunk_size=16)')
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
                union = shingles[line.id] | shingles[other_id]
                overlap = len(shingles[line.id] & shingles[other_id]) / len(union) if union else 0
                score = similarities.get(other_id, 0)
                if overlap >= NGRAM_OVERLAP or score >= EMBEDDING_COSINE:
                    flags.append(Flag(line.id, line.guid, 'member_across_days', other_id, round(max(overlap, score), 6)))
                    break
            for shingle in shingles[line.id]:
                lexical[line.guid, shingle].add(line.id)
            if line.id in vectors:
                conn.execute('INSERT INTO talk_repeat_day VALUES (?,?)', (line.id, day(line.ts)))
                conn.execute('INSERT INTO talk_repeat_vec VALUES (?,?,?)', (line.id, line.guid, vectors[line.id]))
    finally:
        conn.execute('DROP TABLE talk_repeat_vec')
        conn.execute('DROP TABLE talk_repeat_day')
    phrases, stocks, member_counts = defaultdict(list), defaultdict(list), defaultdict(int)
    for line in lines:
        own = tokens[line.id]
        member_counts[line.guid] += 1
        bucket = evening(line.ts)
        if bucket is not None:
            for phrase in ngrams(own, GUILD_PHRASE_WORDS):
                phrases[bucket, phrase].append(line)
        if len(own) >= MIN_WORDS:
            stocks[line.guid, 'stock_opening', own[:STOCK_WORDS]].append(line)
            stocks[line.guid, 'stock_closing', own[-STOCK_WORDS:]].append(line)
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
                       stock_words=STOCK_WORDS, stock_count=STOCK_COUNT, stock_share=STOCK_SHARE)
