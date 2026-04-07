"""
Glicko-2 implementation. Based on Mark Glickman's paper:
http://www.glicko.net/glicko/glicko2.pdf

Ratings are stored in the standard Glicko scale (rating ~1500, rd ~350).
Internally we convert to the Glicko-2 scale (mu, phi) for updates.
"""
import math

SCALE = 173.7178
TAU = 0.5  # system volatility constraint; lower = ratings change more slowly
EPSILON = 0.000001


def _to_g2(rating: float, rd: float) -> tuple[float, float]:
    return (rating - 1500.0) / SCALE, rd / SCALE


def _from_g2(mu: float, phi: float) -> tuple[float, float]:
    return mu * SCALE + 1500.0, phi * SCALE


def _g(phi: float) -> float:
    return 1.0 / math.sqrt(1.0 + 3.0 * phi * phi / (math.pi * math.pi))


def _E(mu: float, mu_j: float, phi_j: float) -> float:
    return 1.0 / (1.0 + math.exp(-_g(phi_j) * (mu - mu_j)))


def update_pair(
    a_rating: float, a_rd: float, a_vol: float,
    b_rating: float, b_rd: float, b_vol: float,
    a_score: float,  # 1.0 if A won, 0.0 if B won, 0.5 for tie
) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    """Update both players after a single head-to-head result."""
    new_a = _update_one(a_rating, a_rd, a_vol, b_rating, b_rd, a_score)
    new_b = _update_one(b_rating, b_rd, b_vol, a_rating, a_rd, 1.0 - a_score)
    return new_a, new_b


def _update_one(
    rating: float, rd: float, vol: float,
    opp_rating: float, opp_rd: float,
    score: float,
) -> tuple[float, float, float]:
    mu, phi = _to_g2(rating, rd)
    mu_j, phi_j = _to_g2(opp_rating, opp_rd)

    g = _g(phi_j)
    E = _E(mu, mu_j, phi_j)
    v = 1.0 / (g * g * E * (1.0 - E))
    delta = v * g * (score - E)

    # Volatility update (Illinois method)
    a = math.log(vol * vol)

    def f(x: float) -> float:
        ex = math.exp(x)
        num = ex * (delta * delta - phi * phi - v - ex)
        den = 2.0 * (phi * phi + v + ex) ** 2
        return num / den - (x - a) / (TAU * TAU)

    A = a
    if delta * delta > phi * phi + v:
        B = math.log(delta * delta - phi * phi - v)
    else:
        k = 1
        while f(a - k * TAU) < 0:
            k += 1
        B = a - k * TAU

    fA = f(A)
    fB = f(B)
    while abs(B - A) > EPSILON:
        C = A + (A - B) * fA / (fB - fA)
        fC = f(C)
        if fC * fB <= 0:
            A, fA = B, fB
        else:
            fA = fA / 2.0
        B, fB = C, fC

    new_vol = math.exp(A / 2.0)

    phi_star = math.sqrt(phi * phi + new_vol * new_vol)
    new_phi = 1.0 / math.sqrt(1.0 / (phi_star * phi_star) + 1.0 / v)
    new_mu = mu + new_phi * new_phi * g * (score - E)

    new_rating, new_rd = _from_g2(new_mu, new_phi)
    # clamp RD
    new_rd = min(max(new_rd, 30.0), 350.0)
    return new_rating, new_rd, new_vol
