from collections import Counter
from random import Random

import pytest

from leeroy_mind.training import bandit as b


def arms(count=8):
    return (b.Arm(1, (0.0,) * count, True), b.Arm(2, (1.0,) * count, False))


def test_posterior_matches_closed_form():
    assert b.posterior((1, 2, 3)) == pytest.approx((1.5, 4, 2.5, 3.5))
    assert b.posterior((), mu0=3, kappa0=2, alpha0=4, beta0=5) == (3, 2, 4, 5)
    assert b.posterior((2, 2), mu0=1, kappa0=2, alpha0=3, beta0=4) == (1.5, 4, 4, 4.5)


def test_draw_uses_gamma_scale_and_precision():
    class Rng:
        def gammavariate(self, alpha, scale):
            assert (alpha, scale) == (3, 0.25)
            return 2

        def gauss(self, mean, sigma):
            assert mean == 1
            assert sigma == pytest.approx(0.5)
            return mean + sigma

    assert b.draw((1, 2, 3, 4), Rng()) == 1.5


def test_top_two_explores_runner_up_and_single_arm():
    candidates = (b.Arm(1, (20.0,) * 80, True), b.Arm(2, (-20.0,) * 80, False))
    rng = Random(123)
    counts = Counter(b.top_two(candidates, rng) for _ in range(400))
    assert 160 <= counts[1] <= 240
    assert 160 <= counts[2] <= 240
    assert b.top_two(candidates[:1], rng) == 1
    assert b.top_two(candidates, rng, beta=1) == 1
    assert b.top_two(candidates, rng, beta=0) == 2


def test_top_two_fallback_uses_best_remaining_posterior(monkeypatch):
    candidates = (b.Arm(1, (100.0,) * 8, False), b.Arm(2, (50.0,) * 8, True),
                  b.Arm(3, (2.0,) * 8, False))
    monkeypatch.setattr(b, "draw", lambda post, rng: post[0])
    assert b.top_two(candidates, Random(1), beta=0) == 2


def test_top_two_fallback_excludes_sampled_leader_when_it_is_not_mean_best(monkeypatch):
    candidates = (b.Arm(1, (100.0,) * 8, True), b.Arm(2, (50.0,) * 8, False),
                  b.Arm(3, (2.0,) * 8, False))
    # Every simulated posterior draw favors arm2 despite arm1's higher mean.
    monkeypatch.setattr(b, "draw", lambda post, rng: 10 if 40 < post[0] < 50 else 0)
    assert b.top_two(candidates, Random(1), beta=0) == 1


def decision_stubs(monkeypatch, probability):
    monkeypatch.setattr(b, "below", lambda *args, **kw: 0.0)
    monkeypatch.setattr(b, "p_best", lambda *args, **kw: {1: 1 - probability, 2: probability})


def test_promote_needs_eight_pulls_on_both_sides(monkeypatch):
    decision_stubs(monkeypatch, 0.99)
    for left, right in ((7, 8), (8, 7)):
        candidates = (b.Arm(1, (0.0,) * left, True), b.Arm(2, (1.0,) * right, False))
        assert b.decide(candidates, Random(1), margin=.8, wipes={}, total_pulls=15).action == "continue"
    result = b.decide(arms(), Random(1), margin=.8, wipes={}, total_pulls=16)
    assert (result.action, result.arm) == ("promote", 2)


def test_two_wipes_precedes_probability_and_champion_is_never_retired(monkeypatch):
    def unexpected(*args, **kw):
        pytest.fail("probability should not be sampled before the two-wipe stop")
    monkeypatch.setattr(b, "below", unexpected)
    result = b.decide(arms(), Random(1), margin=.8, wipes={1: 9, 2: 2}, total_pulls=30)
    assert result == b.Verdict("retire", 2, "two_wipes")


def test_probability_retirement_requires_six_pulls(monkeypatch):
    decision_stubs(monkeypatch, .04)
    assert b.decide(arms(5), Random(1), margin=.8, wipes={}, total_pulls=10).action == "continue"
    assert b.decide(arms(6), Random(1), margin=.8, wipes={}, total_pulls=12).action == "retire"


def test_conservative_stop_and_failure_stop():
    champ = b.Arm(1, (0.0,) * 40, True)
    worse = b.Arm(2, (-1.5,) * 40, False)
    result = b.decide((champ, worse), Random(12), margin=.8, wipes={}, total_pulls=16)
    assert result == b.Verdict("retire", 2, "conservative")
    equal = b.Arm(2, (0.0,) * 40, False)
    fails = (champ, b.Arm(2, (-2.0,) * 40, False))
    result = b.decide((champ, equal), Random(12), margin=.8, wipes={}, total_pulls=16, fail_arms=fails)
    assert result == b.Verdict("retire", 2, "conservative_fail")


def test_decision_checks_all_safety_stops_before_any_promotion(monkeypatch):
    candidates = (*arms(), b.Arm(3, (-3.0,) * 8, False))
    monkeypatch.setattr(b, "below", lambda arm, *args, **kw: .99 if arm.id == 3 else 0)
    monkeypatch.setattr(b, "p_best", lambda *args, **kw: pytest.fail("safety stop comes first"))
    assert b.decide(candidates, Random(1), margin=.8, wipes={}, total_pulls=24).arm == 3


def test_give_up_after_thirty(monkeypatch):
    decision_stubs(monkeypatch, .5)
    assert b.decide(arms(), Random(1), margin=.8, wipes={}, total_pulls=29).action == "continue"
    result = b.decide(arms(), Random(1), margin=.8, wipes={}, total_pulls=30)
    assert result.action == "give_up" and result.arm is None


def test_seeded_probabilities_and_decisions_are_deterministic():
    candidates = arms()
    first = b.p_best(candidates, Random(44))
    assert first == b.p_best(candidates, Random(44))
    assert sum(first.values()) == pytest.approx(1)
    assert first[2] > .9
    assert b.below(candidates[0], candidates[1], .4, Random(1)) > .8
    assert b.decide(candidates, Random(7), margin=.8, wipes={}, total_pulls=16) == b.decide(
        candidates, Random(7), margin=.8, wipes={}, total_pulls=16)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), True, "1"])
def test_invalid_observations_rejected(value):
    with pytest.raises(ValueError):
        b.posterior((value,))


@pytest.mark.parametrize("kwargs", [{"kappa0": 0}, {"alpha0": -1}, {"beta0": float("nan")}, {"mu0": float("inf")}])
def test_invalid_prior_rejected(kwargs):
    with pytest.raises(ValueError):
        b.posterior((), **kwargs)


def test_invalid_arm_identity_and_decision_metadata_rejected():
    for candidates in ((), (b.Arm(1, (), False), b.Arm(1, (), True))):
        with pytest.raises(ValueError):
            b.p_best(candidates, Random(1))
    for candidates in ((b.Arm(1, (), False),), (b.Arm(1, (), True), b.Arm(2, (), True))):
        with pytest.raises(ValueError):
            b.decide(candidates, Random(1), margin=.8, wipes={}, total_pulls=1)
    with pytest.raises(ValueError):
        b.decide(arms(), Random(1), margin=.8, wipes={}, total_pulls=16,
                 fail_arms=(b.Arm(1, (), True), b.Arm(3, (), False)))
    for kwargs in ({"margin": -.1}, {"total_pulls": -1}, {"wipes": {2: -1}}, {"fail_margin": float("nan")}):
        options = dict(margin=.8, wipes={}, total_pulls=16) | kwargs
        with pytest.raises(ValueError):
            b.decide(arms(), Random(1), **options)
    with pytest.raises(ValueError):
        b.p_best(arms(), Random(1), n=0)
    with pytest.raises(ValueError):
        b.top_two(arms(), Random(1), beta=2)
