import math
import os
import random
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))
import cancel_probability as cp  # noqa: E402


class TestCancelProbability(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.events = cp.simulate(3000, seed=3)

    def test_classify(self):
        self.assertEqual(cp.classify("cancel", "bid"), cp.CANCEL_BID)
        self.assertEqual(cp.classify("market", "buy"), cp.MARKET_BUY)
        self.assertEqual(cp.classify("trade", "bid"), cp.MARKET_SELL)
        with self.assertRaises(ValueError):
            cp.classify("foo", "bid")

    def test_auc(self):
        self.assertEqual(cp.auc([0.1, 0.2, 0.8, 0.9], [0, 0, 1, 1]), 1.0)
        self.assertEqual(cp.auc([0.5, 0.5, 0.5, 0.5], [0, 1, 0, 1]), 0.5)

    def test_markov_learns_deterministic_cycle(self):
        evs = [cp.Event(float(i), [cp.LIMIT_BID, cp.CANCEL_BID][i % 2]) for i in range(500)]
        m = cp.Markov().fit(evs)
        m.update(evs[0])  # last = limit_bid -> next should be cancel_bid
        self.assertGreater(m.predict()[cp.CANCEL_BID], 0.95)

    def test_hawkes_distribution_matches_monte_carlo(self):
        h = cp.Hawkes(betas=(2.0,), iters=20).fit(self.events)
        h.reset()
        for e in self.events[:50]:
            h.update(e)
        p = h.predict()
        self.assertAlmostEqual(sum(p), 1.0, places=9)
        # simulate the next event (no new excitation before it): competing clocks
        rng, counts, n = random.Random(0), [0] * cp.K, 20000
        a = h.excitation()
        for _ in range(n):
            t = 0.0
            while True:
                lam = [h.mu[i] + a[i] * math.exp(-h.beta * t) for i in range(cp.K)]
                bar = sum(h.mu) + sum(a) * math.exp(-h.beta * t)
                t += rng.expovariate(bar)
                lam = [h.mu[i] + a[i] * math.exp(-h.beta * t) for i in range(cp.K)]
                if rng.random() * bar <= sum(lam):
                    u, k = rng.random() * sum(lam), 0
                    while k < cp.K - 1 and u > lam[k]:
                        u -= lam[k]
                        k += 1
                    counts[k] += 1
                    break
        for i in range(cp.K):
            self.assertAlmostEqual(p[i], counts[i] / n, delta=0.015)
        self.assertLess(h.branching_ratio(), 1.0)
        self.assertTrue(0 < h.prob_cancel_within(0.1) < h.prob_cancel_within(1.0) < 1)

    def test_run_beats_base_rate_and_csv_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ev.csv")
            cp.save_csv(self.events, path)
            loaded = cp.load_csv(path)
        self.assertEqual(len(loaded), len(self.events))
        self.assertEqual([e.kind for e in loaded], [e.kind for e in self.events])
        r = cp.run(loaded)
        for name, v in r["live"].items():
            self.assertAlmostEqual(sum(v["distribution"].values()), 1.0, places=6)
            self.assertTrue(0 <= v["p_cancel"] <= 1)
        base = r["metrics"]["base_rate"]["log_loss"]
        self.assertLess(r["metrics"]["ensemble"]["log_loss"], base)
        self.assertAlmostEqual(sum(r["ensemble_weights"].values()), 1.0, places=6)


if __name__ == "__main__":
    unittest.main()
