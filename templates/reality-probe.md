# Reality probe: {epic or product name}

> One page, written before any contracts. `direction_gate.py probe-check` only
> confirms that the sections are filled in and that a real source is cited.
> A presence check is not evidence: whoever reads this must open the cited
> source and repeat the hand check. If the baseline already answers the
> success question, the epic has to beat the baseline or it should not be built.

## Success question

{The one question the operator needs answered, in their words. What would they
stop doing by hand if this worked?}

## Real-data hand check

Source: {path, URL, gs:// or bq:// of REAL data, in backticks or as a link. A fixture, mock or sample does not count.}
Method: {what you did by hand: the queries you ran and the files you compared}
Answer: {what the hand check found TODAY, answered as well as it can be without the product}

## Baseline

Method: {the simplest check that could work: a grep, a diff of two configs, one SQL query}
Result: {what that baseline finds. This is the bar the product has to clear}
