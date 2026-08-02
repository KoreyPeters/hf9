# Getting past five questions you can't answer

Five prompts per receipt, always the same five, and no way to say "I don't know
this one". If the top five are unidentifiable, everything behind them is
unreachable for thirty days.

The cap is intended. The dead end is not — nothing in `disambiguation.py`
considers a player who cannot answer the top N.

---

## What is actually happening

`prompt_queue` (`disambiguation.py:74`) sorts pending lines by
`(-blocked_elsewhere, confidence, pk)` and slices to `MatchConfig.prompt_budget`,
default 5. A line leaves that queue in exactly three ways:

1. The player resolves it — confirm, choose, free text, or the new accept.
2. Retro-matching finds a confident match and sets `STATE_NOT_NEEDED`
   (`retro.py:118-122`).
3. The purchase is anonymised at 30 days and the prompt ceases to exist.

There is no fourth. `PurchaseLineItem` has three states — `not_needed`,
`pending`, `resolved` (`models.py:946-953`) — and there is no skip, dismiss or
snooze anywhere in the app.

### Two correct decisions that combine badly

**The pk tiebreak.** `rank` ends in `line.pk` and the comment says why: "purely
so the order is stable between page loads." Right on its own — a list that
reshuffles under you is unusable. It is also precisely what makes the five
inescapable. Refreshing gets you the same five forever.

**The confidence sort.** Unmatched lines carry no confidence, so `rank`
substitutes 0 and they sort ahead of everything
(`disambiguation.py:98`). `test_unmatched_lines_outrank_weak_matches` pins this
as deliberate, and by *global value* it is correct: a line with no match at all
is worth more to resolve than one with a shaky match.

But it means the five you are shown are systematically **the least identifiable
lines on the receipt**, while the weak matches — "We matched this to Heinz
Ketchup. Does that look right?", answerable in one tap without thinking — are
exactly what gets buried. The queue surfaces the hardest questions and hides the
cheap ones. That is the mechanism behind "I can't move on to the ones I do know."

### The budget quietly governs work as well as attention

Not a performance objection to anything below — the numbers are small. It is an
undocumented coupling, and worth writing down before somebody trips on it.

`prompt_queue` sorts, **slices to the budget, and only then** matches
(`disambiguation.py:101-107`). So the page runs exactly `prompt_budget` calls to
`matching.match_line_item` per render, whether the receipt has six pending lines
or sixty. Each call is an FTS narrowing query plus a fuzzy scoring pass.

Those calls exist because `Prompt.candidates` is recomputed at display time
rather than reused from processing — deliberate, and right: the catalogue may
have grown since the receipt was read, so the better answer might now exist. The
`Prompt` docstring says recomputing "costs nothing — the matching cascade is
offline."

**That is true because of the budget, and the docstring does not mention the
budget.** The two are load-bearing on each other with nothing recording it.

Magnitude, stated honestly so nobody over-reacts to this: `matching.py:95-97`
puts ~6k comparisons at about 12ms, which at `candidate_limit = 200` is roughly
thirty lines' worth — call it 0.4ms of scoring per line. Forty lines is ~16ms.
The less-measured part is forty FTS `narrow` queries instead of five, and FTS5 is
fast. Tens of milliseconds, not a problem.

The reason to record it anyway is that it can already bite with no code change:
`prompt_budget` is admin-editable (`models.py:719`). Setting it to 50 in the
admin today multiplies per-page matching work, and nothing in the field's help
text, the docstring, or the admin suggests it does anything but change how many
questions appear.

---

## Options

### A. "Show the rest" — recommended

A toggle that re-renders the section with the budget lifted for this receipt.

```python
def prompt_queue(purchase: Any, limit: int | None = None) -> list[Prompt]:
    ...
    budget = config.prompt_budget if limit is None else limit
```

The view passes `limit` from a query parameter or signal; the partial renders a
"Show the other N" link when `pending > budget`.

- **No migration, no new state, no change to what `pending` means.** Everything
  reading `STATE_PENDING` — metrics, Action Centre, the receipt list's
  `unresolved` annotation — keeps working untouched.
- **It answers the actual complaint directly.** One click and the easy ones are
  visible; no tapping through five unknowns to reach them.
- **It preserves what the budget is for.** The docstring's concern is a player
  "shown fifteen icons" by default. A player who asks for fifteen has opted in,
  and that is a different thing.

Cost: the matching call per prompt, as above — tens of milliseconds on a large
receipt, so almost certainly fine as-is. Worth an explicit expansion cap anyway,
because "almost certainly fine" is an assumption about receipt sizes we have not
seen yet. The fallback if it ever does matter is to skip candidate recomputation
for the expanded set and render those with their stored matches.

### B. Skip — the better answer, and more work

A `skipped_at` timestamp on `PurchaseLineItem`, with `prompt_queue` ordering
skipped lines **last** rather than excluding them.

The reason to prefer a timestamp over a fourth state is the metrics. `prompt_rate`
counts `STATE_PENDING` over all lines and `prompt_completion_rate` computes
`resolved / (pending + resolved)` (`metrics.py:80`, `metrics.py:114-127`). Add a
`STATE_SKIPPED` and completion rate **rises every time a player fails to answer**
— the metric improves as the system gets worse at being answerable, which is the
worst possible direction for a number whose whole job is to detect prompts being
ignored. A timestamp leaves every existing query correct.

Ordering last rather than excluding also means skipping everything degrades
gracefully: the list refills with the oldest skips instead of going empty and
hiding work that still exists.

It also captures signal worth having. "Nobody who bought this could identify it"
is different from "nobody has been asked yet", and only B can tell them apart.

### C. Reorder to interleave easy wins — not recommended

Reserve part of the budget for weak matches so one-tap confirmations always
appear. Tempting, and it needs no new interaction at all.

Against it: the global-value ordering is well argued and load-bearing — a string
blocking five hundred line items across the player base genuinely is worth five
hundred times a private oddity, and this would demote it in favour of whatever is
easiest. It is also a heuristic with a ratio to tune and no obvious right answer,
and it does nothing at all when every pending line is hard. The problem is
access, not order.

---

## Recommendation

**A now, B if it still bites.** A is a view and a template, solves the stated
problem completely, and cannot break anything that reads the state field. B is
better UX and produces a signal we do not currently collect, but it needs a
migration and careful handling of two metrics, and it is worth knowing whether A
is sufficient before paying for that.

If both eventually land they compose cleanly: skip moves a line down, "show the
rest" reveals everything regardless.

---

## What I am deliberately not proposing

- **Raising `prompt_budget`.** It is admin-editable already, so this needs no
  code — but it trades the dead end for the fifteen-icons problem the budget
  exists to prevent, on every receipt, for every player.
- **Touching the ordering.** Option C, argued above.
- **Anything in the Action Centre.** Debt item 11 stands as recorded; this plan
  does not change it either way.

---

## Todo steps

**Option A** — done 2026-08-02.

- [x] Add a `limit` parameter to `prompt_queue`, defaulting to the configured
      budget. Existing callers unchanged.
- [x] Add `total_pending` to the prompt context so the template knows whether
      there is anything behind the cap, without counting `prompts` itself.
      Landed as `hidden_count`, which is what the template actually needs and
      keeps the arithmetic in one place.
- [x] Thread it through `_prompts_ctx` / `_prompts_response`
      (`views.py:146-171`) and `disambiguation_section` so expansion survives a
      re-render after an answer — otherwise answering one prompt collapses the
      list back to five, which would be worse than not offering it.
- [x] Add the toggle to `disambiguation_section.html`, shown only when
      `total_pending > budget`. "Show fewer" added alongside it, so an expanded
      list is not a one-way door.
- [x] Test: a receipt with 12 pending lines shows 5 by default and 12 expanded.
- [x] Test: answering a prompt while expanded re-renders still expanded. Watch
      this one fail first — it is the whole reason the state has to be threaded
      through the partial rather than read once in the view.
- [x] Test: the toggle is absent when pending fits inside the budget, and when
      prompting is disabled outright.
- [x] Decide and record the expansion cap, if any, against the matching cost
      above. **No cap** — see below.
- [x] Note the budget/recomputation coupling where a reader will hit it — the
      `Prompt` docstring's "costs nothing" and `prompt_budget`'s help text are
      the two places that currently imply otherwise. Both rewritten;
      `0019_alter_matchconfig_prompt_budget` carries the help-text change.

### The expansion cap: none, deliberately

A cap would recreate the bug. Expanding a 60-line receipt to a hidden ceiling of
50 leaves ten questions unreachable with no further affordance — the same dead
end, one step further along. Given the cost is tens of milliseconds, an unbounded
expansion is the safer of the two, and the fallback if a pathological receipt
ever turns up is to render the expanded set from stored matches rather than
recomputing candidates.

### Watching the guards fail

`test_answering_while_expanded_stays_expanded` carries two assertions and each
was verified against its own failure mode:

- Making `_expanded` ignore POSTs collapsed the list to five — caught by the
  count assertion.
- Hardcoding `"prompts_expanded": false` in the partial's `data-signals` left the
  list expanded but declared it collapsed, so the *next* answer would have
  snapped back — caught only by the second assertion. This is the subtler of the
  two bugs and would have shipped looking fine.

**Decisions for Korey**

- [x] A alone, or A then B? **A alone.** B is not being built until it is needed.
- [x] Cap the expansion, or show everything pending however many there are?
      **Everything**, per the reasoning above.
