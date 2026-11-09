# quant.bot

## Cancel probability (`bot/cancel_probability.py`)

Estimates the probability that the **next limit-order-book event is a cancellation**
(and on which side: bid or ask), using several quant models side by side:

| Model | Idea |
|---|---|
| `base_rate` | Static class frequencies: the benchmark to beat |
| `ewma` | Exponentially weighted frequencies that adapt to the current regime |
| `markov` | Order-2 Markov chain on event types with Dirichlet backoff |
| `hawkes` | Multivariate self-exciting Hawkes process (MLE via EM). Exact next-event probabilities plus P(cancel within *h* seconds) |
| `logistic` | Softmax regression on queue sizes, imbalance, spread, time since last event and recent event mix |
| `ensemble` | Linear pool of all models, weights fitted on a validation slice |

Models are trained on the first 70% of events and scored on the unseen 30%
(log loss, Brier, AUC, accuracy). Pure Python, no installs needed.

```
python bot/cancel_probability.py                    # demo on a synthetic order book
python bot/cancel_probability.py --csv events.csv   # your own data
python bot/cancel_probability.py --save-sample sample.csv
python bot/cancel_probability.py --json
python -m unittest discover tests
```

CSV columns: `timestamp, event (limit|cancel|market), side (bid|ask, or buy|sell for market),
bid_size, ask_size, spread` (book columns are optional and describe the book *after* the event).

Research tool only, not financial advice.
