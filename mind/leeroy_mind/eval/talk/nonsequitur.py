"""Relevance of conservative directed-thread reply candidates, never invented links."""
from collections import defaultdict, deque

from .common import Flag, similarity, words

PARENT_COSINE = 0.20
THREAD_COSINE = 0.25
CONTEXT_SECONDS = 300
THREAD_LINES = 5
MIN_REPLY_WORDS = 4
FILLERS = frozenset({'lol', 'lulz', 'lmao', 'rofl', 'haha', 'hehe'})


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
                if question and tokens and len(tokens) <= 3 and set(tokens) <= FILLERS:
                    flags.append(Flag(line.id, line.guid, 'filler_after_question', parent.id))
                elif len(tokens) >= MIN_REPLY_WORDS and line.id in vectors and parent.id in vectors:
                    sims = [similarity(vectors[line.id], vectors[p.id]) for p in history if p.id in vectors]
                    parent_score = similarity(vectors[line.id], vectors[parent.id])
                    checked += 1
                    if parent_score < PARENT_COSINE and sims and max(sims) < THREAD_COSINE:
                        flags.append(Flag(line.id, line.guid, 'low_parent_and_thread_relevance', parent.id,
                                          round(parent_score, 6)))
        history.append(line)
    return flags, dict(stored_parent_links=0, directed_parent_candidates=candidates,
                       lines_with_thread_context=thread_lines, relevance_checked=checked,
                       directed_questions=questions, parent_cosine=PARENT_COSINE,
                       thread_cosine=THREAD_COSINE, context_seconds=CONTEXT_SECONDS,
                       thread_lines=THREAD_LINES, min_reply_words=MIN_REPLY_WORDS)
