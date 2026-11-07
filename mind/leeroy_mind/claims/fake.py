"""Deterministic FakeLLM replies for CLI smoke evaluation, not an accuracy model."""
import json
import re

from .extract import MAX_CLAIMS
from .stance import assertions, polarity


def reply(model, messages) -> str:
    data = json.loads(messages[-1]['content'].split('\n\n', 1)[0])
    claims = []
    for line in data['lines']:
        for text in assertions(line['text']):
            match = re.fullmatch(r'I (love|hate|like|dislike|enjoy) (.+)', text, re.I)
            job = re.fullmatch(r'I work at (.+)', text, re.I)
            if match:
                verb, subject = match.groups()
                claim = dict(kind='opinion', subject=subject.lower(),
                             text=f'{verb.lower()}s {subject}', polarity=polarity(text))
            elif job:
                claim = dict(kind='fact', subject='her job',
                             text=f'works at {job[1]}', polarity=0)
            else:
                continue
            claims.append(dict(**claim, horizon_days=None, source_line=line['id']))
    return json.dumps({'claims': claims[:MAX_CLAIMS]})
