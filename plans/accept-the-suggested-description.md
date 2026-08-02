# Accepting the reading we already show you

Three questions, then a feature. The questions matter more than the feature,
because two of them have answers that are already in the code and are not what
the interface implies.

---

## 1. What happens if you don't describe it?

It depends which of the two prompts you are looking at, and they behave in
opposite ways. `disambiguation_section.html:36` branches on `prompt.has_match`.

### Weak match — "We matched this to X. Does that look right?"

**We have already accepted it.** `line.product` is set, and has been since
processing. It counts toward `product_points` (`points.py:71-79`) and toward that
product's rating. Not answering is not a deferral; it is silent agreement.

This branch already has the button you are asking for — "Yes, that's it"
(`disambiguation_section.html:43`) — and what it changes is not whether we use
the match, but who is on record as having decided it: `match_tier` becomes
`PLAYER`, and retro-matching will never revisit the line again
(`retro.py:73`).

### No match — "We couldn't place this one. We read it as *X*."

**Nothing happens, and nothing ever will.** `line.product` is `None`. The line
earns no product points and feeds no product's rating. The interpreted name is
displayed and then dropped on the floor.

Not *entirely* dropped, in fairness: `interpreted_name` is a stored field and is
passed into the matching cascade every time the line is re-examined
(`disambiguation.py:106`, `retro.py:91`), so it keeps working as a matching
*input*. But it never becomes a product, never becomes an alias, and never
resolves the line on its own.

**This is the gap you found.** The interface shows you a reading and offers you
no way to agree with it. The only routes forward are the candidate dropdown —
which is often empty, because nothing cleared the noise floor, which is why we
are in this branch at all — and typing out by hand a description already printed
on the screen in front of you.

### A consequence worth being explicit about

Resolving a line **never pays you more**. `award_for_purchase` is guarded on
`points_awarded` (`points.py:144`) and retro-matching is forbidden from touching
settled points (`retro.py:20-22`). The points were settled when the receipt was
read.

So every one of these prompts is a request for unpaid work that benefits other
players. That is a defensible design — the header already says "Only if you have
a moment" — but it sets a hard ceiling on how much friction any answer can carry,
and it is the strongest argument for the Accept button existing.

---

## 2. How long do they present themselves?

**30 days, and then they vanish without a word.**

`Purchase.anonymise_after` is set at creation to
`now + SPENDIUM["PURCHASE_RETENTION_DAYS"]` (`models.py:929-933`, currently 30).
`window_is_open` compares against it (`models.py:936`), and
`_require_open_window` raises `WindowClosedError` past it
(`disambiguation.py:120`).

At that point `anonymise_purchase` (`service.py:550`) copies each line into an
`AnonymisedLineItem` — carrying `raw_text`, `interpreted_name`, `product`,
`match_tier` — and deletes the original. `disambiguation_state` is **not** among
the copied fields, so the prompt does not expire so much as cease to exist.

Nothing anywhere tells you this is coming. Not the prompt, not the Action Centre,
not the receipt list.

## 3. Should they expire?

They already do. The real questions are whether the deadline should be *visible*,
and whether anything should happen when it passes.

**Visible: yes.** It costs one line of template and it is the honest thing to do
when the ask is "only if you have a moment" — a moment that turns out to have an
end date.

**Auto-accept at the deadline: no, and firmly.** This is the tempting version of
your feature and I think it is a trap. `_resolve` stamps `MatchTier.PLAYER`, and
`retro._should_skip` treats a `PLAYER` line as untouchable forever, on the
reasoning that "they held the product; no amount of later string similarity
outranks that." Auto-accepting would attach that authority to a reading **nobody
ever looked at**, and permanently exempt it from the retro-matching that would
otherwise have fixed it for free as the catalogue grew.

The current behaviour is better than it looks: an unresolved line survives
anonymisation and keeps being retro-matched indefinitely (`retro.py:150-160`,
and the module docstring is explicit that anonymous lines are included on
purpose). Doing nothing at the deadline costs a prompt. Auto-accepting costs the
correction mechanism.

**Expire sooner: no**, but see the Action Centre problem below, which is the real
version of that worry.

---

## The feature

Add an Accept button to the no-match branch. The mechanism already exists:

```python
# spendium/disambiguation.py
def accept_reading(line: PurchaseLineItem) -> PurchaseLineItem:
    """The player agrees with the name we read off the receipt.

    Deliberately routed through the same cascade as a typed description rather
    than straight into the catalogue. The player is agreeing with the words, not
    asserting that no existing record already means them — and `submit_free_text`
    is where that distinction is already handled.
    """
    _require_open_window(line)
    if not line.interpreted_name:
        raise ValueError("There is no reading to accept.")
    return submit_free_text(line, line.interpreted_name)
```

That is the whole of it. `submit_free_text` (`disambiguation.py:214`) already
does the right things: runs the text through matching so an existing record wins
over a near-duplicate, and only mints a new unverified `Product` via
`create_or_cluster` when nothing matches.

A view at `spendium/views.py` mirroring `confirm_line`, a URL, and a button in
the `{% else %}` branch of `disambiguation_section.html:50-57`.

### The decision I want you to make

**Should one tap carry the same weight as typing it out?**

As written above, it does. `submit_free_text` → `_resolve` sets
`match_tier = PLAYER` and calls `_apply_alias`, which casts a full player vote
toward a confirmed alias. Two players accepting the same reading promotes it
(`ALIAS_CONFIRMATIONS_REQUIRED = 2`), after which it matches silently for
everyone forever.

Arguments that this is fine:

- **The blast radius is already bounded.** One vote confirms nothing; promotion
  needs two *distinct* players, and `_apply_alias` demotes on contradiction.
- **It introduces no new failure mode.** A player can already mint a junk product
  by typing junk into the free-text box. Accept makes the existing path one tap
  cheaper, it does not open a new one.
- **The precedent is right there.** "Yes, that's it" is already one tap for full
  player authority on a weak match.

The argument that it is not fine, which I think is real but not decisive:

- Confirming a weak match links a line to a catalogue record **that already
  exists** and was matched by score. Accepting a reading can **mint a new product
  record** out of LLM text with a player's authority stamped on it. Those are
  different acts, and only the second one can put something into the catalogue
  that nobody has ever checked.
- Your own framing — "the descriptions are often quite accurate" — describes
  exactly the regime where a one-tap button gets rubber-stamped without reading.
  Accuracy that is usually right is what trains people to stop looking.

**My recommendation: ship it as one tap, identical to free text.** One code path,
consistent with the existing button, and the alias layer's two-player threshold
is the defence that makes it safe. But watch the unverified-product count in
`metrics` after it ships — if Accept starts minting products faster than
`create_or_cluster` clusters them, the cheap correction is to require that Accept
only resolves into an *existing* record and falls back to the current behaviour
otherwise.

---

## The Action Centre problem, which this makes worse

Worth raising here because Accept will make people go looking for these.

`prompt_queue` is budgeted to `MatchConfig.prompt_budget` (default 5) per
receipt, and the field's own help text explains why: "Players who see fifteen
icons ignore all of them."

`action_centre.unresolved_disambiguations` (`action_centre.py:84-91`) has **no
budget at all**. It returns every pending line the player has, and
`action_centre.html:49` renders all of them. The exact failure the per-receipt
budget exists to prevent is reachable in one click from the same interface, and
it gets worse with every receipt uploaded.

I have not fixed it here because capping it is a judgement about the Action
Centre rather than about this feature, and because the right cap probably is not
5 — a page the player chose to open can reasonably show more than a receipt they
were merely looking at. Flagging it as the thing most likely to undermine the
button being useful.

---

## What I am deliberately not doing

- **"Accept all".** The obvious next request, and the one that turns this from a
  cheap signal into noise. A player who accepts five readings one at a time has
  looked at five; a player who taps "Accept all" has looked at none, and the
  system cannot tell the difference — but it stamps `MatchTier.PLAYER` on all of
  them either way.
- **Auto-accept at expiry.** Argued above.
- **Accept in the Action Centre.** It currently only links through to the
  purchase, and adding inline resolution there is a bigger change than adding a
  button to a prompt that already exists. Worth doing second, and worth doing —
  say if you want it in scope now.
- **Changing what the weak-match branch does.** It already has its button.

---

## Todo steps

- [x] Add `accept_reading` to `spendium/disambiguation.py`, delegating to
      `submit_free_text`.
- [x] Add `accept_line_reading` view mirroring `confirm_line`
      (`spendium/views.py:195`), and a URL in `spendium/urls.py`. The view reads
      no signals — see the note below on why that turned into a test.
- [x] Add the button to the `{% else %}` branch of
      `templates/spendium/partials/disambiguation_section.html`, only when
      `prompt.line.interpreted_name` is non-empty. Reword that block so the
      reading reads as an offer rather than an aside — currently "We read it as
      *X*" is a parenthetical after "We couldn't place this one."
- [x] Show the deadline. `purchase.anonymise_after` is already on the object; the
      prompt header is the place, and the wording should say we stop asking, not
      that anything is lost.
- [x] Test: accepting a reading resolves the line, and to the *existing* product
      when one already matches the reading — the clustering path, not a new
      record.
- [x] Test: accepting a reading with no catalogue match mints exactly one
      unverified product, and a second player accepting the same string on their
      own receipt promotes the alias rather than creating a second product.
- [x] Test: a line with an empty `interpreted_name` offers no Accept button and
      the endpoint rejects it.
- [x] Test: accepting past the window raises `WindowClosedError` and changes
      nothing. Watch this one fail first — `_require_open_window` is easy to
      leave out of a new entry point, and nothing else would catch it.
      **It did not fail**, which was the useful part: see below.
- [x] Test: accepting does not award further points. Pins the answer to question
      1 so a later change to `award_for_purchase` cannot quietly turn prompts
      into a payout.

### What watching the guards fail turned up

- **The window test was vacuous as first written.** Deleting
  `_require_open_window` from `accept_reading` left it green, because
  `submit_free_text` checks the window too — so it was testing the delegate, not
  the new entry point. Split in two: the end-to-end test now says plainly what it
  does not prove, and `test_accept_checks_the_window_itself` stubs the delegate so
  the redundant guard is the only thing that can raise. That one does fail when
  the guard is removed.
- **The clustering guards do bite.** Replacing the `submit_free_text` delegation
  with a direct `Product.objects.create` failed both clustering tests, including
  the two-player case splitting into duplicate records.
- **Two project guards caught real mistakes.** The multi-line `{# … #}` comment I
  wrote would have rendered to the page — exactly the bug `f16bee9` was about —
  and `test_every_route_has_a_declared_audience` refused the new route until it
  was declared `OWNER_ONLY` in `spendium/test_security.py`.
- **Added, unplanned:** a test that the endpoint ignores posted text. Writing the
  view made it obvious that an accept endpoint taking a `free_text` signal would
  be a second, cheaper-looking route for putting arbitrary text into the
  catalogue behind a button labelled as agreement.

**Decisions for Korey**

- [x] One tap = full player authority, or the weaker variant? **Full**, as
      recommended.
- [x] Cap `unresolved_disambiguations`, and at what number? **Not now** — Korey
      will say if it gets annoying. Recorded as debt rather than dropped.
- [x] Accept in the Action Centre now, or later? **Not now.** Clicking through to
      the receipt is fine.

**Register the debt**

- [x] Add the unbudgeted Action Centre disambiguation list to
      `plans/operational-debt.md` if it is not being fixed in this piece of work.
      **Item 11.**
