"""Relevance of conservative directed-thread reply candidates, never invented links.

Every function here is pure (no SQL, no writes) and safe on a live read connection;
`detect` is a batch loop over `score_reply`. Thresholds were chosen against FakeLLM's
word-hash vectors, which only share vocabulary: with a real embedding model they must
be recalibrated before they gate anything.
"""
from collections import defaultdict, deque

from .common import Flag, similarity, words

PARENT_COSINE = 0.20
THREAD_COSINE = 0.25
CONTEXT_SECONDS = 300
THREAD_LINES = 5
MIN_REPLY_WORDS = 4
FILLERS = frozenset({'lol', 'lulz', 'lmao', 'rofl', 'haha', 'hehe'})


def is_filler(tokens):
    return bool(tokens) and len(tokens) <= 3 and set(tokens) <= FILLERS


def relevance_scores(vector, parent_vector, thread_vectors):
    """(cosine to the parent, best cosine to any thread line) for one draft's vector."""
    sims = [similarity(vector, other) for other in thread_vectors]
    return similarity(vector, parent_vector), max(sims) if sims else None


def score_reply(text, vector, parent_text, parent_vector, thread_vectors):
    """Score one draft against its parent and thread; (rule or None, parent score).

    `thread_vectors` are vectors of the recent thread lines (parent included).
    """
    tokens = words(text)
    if parent_text.rstrip().endswith('?') and is_filler(tokens):
        return 'filler_after_question', None
    if len(tokens) >= MIN_REPLY_WORDS and vector is not None and parent_vector is not None:
        parent_score, thread_best = relevance_scores(vector, parent_vector, thread_vectors)
        if parent_score < PARENT_COSINE and thread_best is not None and thread_best < THREAD_COSINE:
            return 'low_parent_and_thread_relevance', parent_score
    return None, None


def detect(lines, context, vectors):
    selected = {line.id for line in lines}
    threads = defaultdict(lambda: deque(maxlen=THREAD_LINES))
    flags, candidates, thread_lines, checked, questions = [], 0, 0, 0, 0
    for line in context:
        if line.thread_id is None:
            continue
        history = threads[line.thread_id, line.channel]
        while history and line.ts - history[0].ts > CONTEXT_SECONDS:
            history.popleft()
        if line.id in selected and history:
            thread_lines += 1
            parent = history[-1]
            # No general stored parent exists. Only reciprocal directed adjacency
            # is considered a parent candidate; guild-channel adjacency is ambiguous.
            directed = (line.to_guid == parent.guid and parent.to_guid == line.guid
                        and line.guid != parent.guid and line.to_guid > 0)
            if directed:
                candidates += 1
                tokens = words(line.text)
                question = parent.text.rstrip().endswith('?')
                questions += int(question)
                embedded = (len(tokens) >= MIN_REPLY_WORDS and line.id in vectors and parent.id in vectors
                            and not (question and is_filler(tokens)))
                checked += int(embedded)
                rule, parent_score = score_reply(
                    line.text, vectors.get(line.id), parent.text, vectors.get(parent.id),
                    [vectors[p.id] for p in history if p.id in vectors])
                if rule == 'filler_after_question':
                    flags.append(Flag(line.id, line.guid, rule, parent.id))
                elif rule:
                    flags.append(Flag(line.id, line.guid, rule, parent.id, round(parent_score, 6)))
        history.append(line)
    return flags, dict(stored_parent_links=0, directed_parent_candidates=candidates,
                       lines_with_thread_context=thread_lines, relevance_checked=checked,
                       directed_questions=questions, parent_cosine=PARENT_COSINE,
                       thread_cosine=THREAD_COSINE, context_seconds=CONTEXT_SECONDS,
                       thread_lines=THREAD_LINES, min_reply_words=MIN_REPLY_WORDS)
