import numpy as np
import pytest

from hhd.sim.states import Cat, Coherent, Fock, Thermal, Vacuum, loss_channel, make_state

N = 200_000


@pytest.mark.parametrize("st", [Vacuum(), Coherent(alpha=1.2 * np.exp(0.4j)), Thermal(nbar=0.3), Fock(n=1), Fock(n=2),
                                Cat(alpha=1.5, parity=1), Cat(alpha=1.2, parity=-1)])
@pytest.mark.parametrize("eta", [1.0, 0.7])
def test_sample_moments_match_density_matrix(st, eta):
    rng = np.random.default_rng(3)
    th = 0.9
    x = st.measure(rng, np.full(N, th), eta)
    e = st.expected(eta, theta=th)
    assert x.mean() == pytest.approx(e["mean_x"], abs=5 * np.sqrt(e["var_x"] / N))
    assert x.var() == pytest.approx(e["var_x"], rel=5 * np.sqrt(2 / N) + 0.01)


def test_w00_analytic():
    for eta in (1.0, 0.7, 0.4):
        assert Fock(1).expected(eta)["W00"] == pytest.approx((1 - 2 * eta) / np.pi)
        assert Fock(2).expected(eta)["W00"] == pytest.approx((1 - 2 * eta) ** 2 / np.pi)
        assert Thermal(0.5).expected(eta)["W00"] == pytest.approx(1 / (np.pi * (2 * 0.5 * eta + 1)))
        assert Coherent(alpha=1.1).expected(eta)["W00"] == pytest.approx(np.exp(-2 * 1.21 * eta) / np.pi, rel=1e-6)
    assert Vacuum().expected()["W00"] == pytest.approx(1 / np.pi)
    assert Cat(alpha=1.5, parity=1).expected()["W00"] == pytest.approx(1 / np.pi)      # gerade Parität
    assert Cat(alpha=1.5, parity=-1).expected()["W00"] == pytest.approx(-1 / np.pi)


def test_loss_channel_trace_preserving():
    r = loss_channel(Cat(alpha=1.3).rho().astype(complex), 0.6)
    assert np.trace(r).real == pytest.approx(1.0, abs=1e-9)
    assert np.allclose(r, r.conj().T)


def test_coherent_phase_dependence():
    st = Coherent(alpha=1.0)
    rng = np.random.default_rng(1)
    th = rng.uniform(0, 2 * np.pi, N)
    x = st.measure(rng, th, 1.0)
    A = 2 * np.mean(x * np.cos(th))
    assert A == pytest.approx(np.sqrt(2), abs=0.02)


def test_make_state():
    assert isinstance(make_state("fock1"), Fock) and make_state({"type": "fock", "n": 2}).n == 2
    with pytest.raises(ValueError):
        make_state({"type": "unbekannt"})   # ab 0.14 gibt es „squeezed“
    with pytest.raises(ValueError):
        make_state({"type": "coherent", "beta": 1})
