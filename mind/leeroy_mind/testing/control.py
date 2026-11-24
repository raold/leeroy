"""The mind's control endpoint, as its test doubles must keep it: a request is built and parsed
by the endpoint's own code (endpoint.request_line, endpoint.parse_request), so a fake refuses
what the mind refuses, in the mind's words (endpoint.refusal); a scripted answer crosses the
wire as a real one does (endpoint.encode, then endpoint.decode_reply), so a fake cannot answer
what the mind could not send.

Every breach is kept in `breaches`; the test suite's conftest fails the test that made one, so
a fake that the console's code talks past (a wrong op, an argument the endpoint refuses, an
answer with no ok) is never a silent pass."""
from leeroy_mind import endpoint

KEY = "k" * 43  # the size of secrets.token_urlsafe(32), the mind's own key

breaches: list[str] = []


class ContractError(AssertionError):
    """A double was asked for what the real endpoint cannot do."""


def _breach(text: str) -> ContractError:
    breaches.append(text)
    return ContractError(text)


def verify() -> None:
    """A ContractError naming every breach since the last verify, which are then forgotten."""
    found, breaches[:] = list(breaches), []
    if found:
        raise ContractError("control endpoint contract broken: " + "; ".join(found))


def refusal(op, args=None, *, key: str = KEY) -> dict | None:
    """The endpoint's answer refusing this request, or None when it would take it. A request
    that cannot be sent at all (not JSON) is a ContractError."""
    try:
        line = endpoint.request_line(key, op, args)
    except (TypeError, ValueError) as e:
        raise _breach(f"control {op!r}: the request cannot be sent ({type(e).__name__}: {e})") \
            from None
    try:
        endpoint.parse_request(line, key)
    except endpoint.BadRequest as e:
        _breach(f"control {op!r} {args!r}: the endpoint refuses it ({e})")
        return endpoint.refusal(e)
    return None


def wire(reply, op="?") -> dict:
    """reply as the console receives it: encoded as the endpoint encodes its answers and read as
    endpoint.request reads them (tuples become lists, keys become strings). ContractError for
    an answer the endpoint could not give (not JSON, over MAX_REPLY, no boolean ok)."""
    try:
        return endpoint.decode_reply(endpoint.encode(reply))
    except (TypeError, ValueError) as e:
        raise _breach(f"control {op!r}: a scripted answer the endpoint cannot give "
                      f"({type(e).__name__}: {e}): {reply!r:.200}") from None


def answer(op, args=None, reply=None, *, key: str = KEY) -> dict:
    """What MindLink.call returns for (op, args) from an endpoint whose handler answers `reply`
    (or reply(op, args), when it is callable): the endpoint's own refusal for a request it
    refuses, else the reply as it crosses the wire."""
    refused = refusal(op, args, key=key)
    if refused is not None:
        return refused
    return wire(reply(op, args) if callable(reply) else reply, op)


def request(reply):
    """A stand-in for endpoint.request (MindLink's `request=`) whose handler answers `reply`, as
    answer() gives it. Its calls are kept in .calls: (port, key, op, args)."""
    def fake(port, key, op, args=None, *, timeout):
        fake.calls.append((port, key, op, args))
        return answer(op, args, reply, key=key)
    fake.calls = []
    return fake
