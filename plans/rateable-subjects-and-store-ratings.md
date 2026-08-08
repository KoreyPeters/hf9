# Rateable subjects, and store ratings

## What was asked

Take stock of everything HF can rate, look at how rating actually works today for
politicians and products, and plan the equivalent for stores.

## Summary

The survey engine is already generic — `SurveyResponse` attaches to any model
through a GenericForeignKey, so nothing needs registering. Two subjects use it
today, in noticeably different ways, and a third (`Store`) is **half built**:
the points half exists and is tested, the player-facing half does not exist at
all.

The interesting finding is not that store rating is missing — `plans/regression-and-gap-discovery.md:32`
already records that. It is that **`surveys.Category` cannot say what kind of
thing its questions are about**, and that limitation is invisible while each game
has exactly one rateable subject. Spendium is about to have two. That is the part
of this plan that is structural rather than additive, and it should land first.

Two further corrections came out of the review (§6) and are now in scope:

- **Product rating wrongly requires a purchase.** `spendium/views.py:346` gates
  `can_rate` on `player_has_bought`, contradicting `design.md:341`. Anyone signed
  in should be able to rate; a purchase decides `is_verified`, not eligibility.
- **The display gate counts verified responses only**, so under the fix above a
  much-rated store would show nothing until someone uploaded a receipt. It moves
  to counting all responses, with the score still weighted.

And one thing found but deliberately *not* fixed here: `criteria_version` is
written by three call sites and read by none, so the guarantee in its own help
text does not exist. Logged as debt item 9.

---

## 1. What is rateable today

| Subject | Game | Survey UI | Rating shown as | Aggregated by | Who may rate |
|---|---|---|---|---|---|
| `Candidate` | polium | `polium/views.py:1070` | % on profile, denormalised to `Candidate.current_rating` | `surveys/ratings.py:11` `compute_rating` | anyone signed in |
| `Product` | spendium | `spendium/views.py:378` | % on `product_detail` | `spendium/ratings.py:105` `compute` | anyone signed in; **verified** only if they bought it |
| `Store` | spendium | **none** | **nothing** | `surveys/ratings.py:40` via points only | — |
| `Manufacturer` | spendium | none (derived) | none | `spendium/ratings.py:177` | n/a — rolls up its products |

Two aggregation formulas coexist, deliberately:

- **`compute_rating`** (`surveys/ratings.py:11`) — weighted mean of yes/no
  answers, returns 0–1. This is the *percentage* players see.
- **`compute_declaration_points`** (`surveys/ratings.py:40`) — Σ (weight ×
  yes-probability) over criteria clearing the k-threshold, returns points per
  dollar, uncapped. This is the *reward*.

They are not two implementations of one idea. The percentage is a judgement; the
points-per-dollar is a payout, and the design (`local_only/design.md:325`) is
explicit that for a brand it is the ppd figure that should be shown prominently,
because "29 points per dollar versus 4" is the signal that moves behaviour.
Products currently show only the percentage. Stores should show ppd.

### Where Polium and Spendium diverge

Polium reads **every** active criterion in the game and groups by category
(`polium/views.py:282-290`). Spendium reads **one** category, chosen as the
lowest-pk one for the game (`spendium/views.py:345`):

```python
category = Category.objects.filter(game="spendium").order_by("pk").first()
```

Both work today only because each game has one rateable subject. Neither
survives a second one.

---

## 2. The blocking problem: categories have no subject

`surveys.Category` (`surveys/models.py:8`) carries `name`, `description`, `game`,
`criteria_version`, `criteria_are_provisional`. There is no field saying *what
this set of questions is about*.

"Does the manufacturer treat its workers fairly?" is a product question. "Are
staff at this chain paid a living wage?" is a store question. Nothing in the
schema distinguishes them, and the selector above resolves the ambiguity by
primary key order.

**This is latent, not live.** Seed order today gives "Product ethics" the lowest
spendium pk, so products get the right questions. But:

- Adding "Store ethics" and having the store view use the same `.first()` idiom
  would silently serve product questions on store pages.
- `spendium/test_points.py:63` already creates a spendium category called
  "Ethics". In any database where that lands at a lower pk, `_rating_ctx` picks
  it and the product survey renders the wrong questions. That is the failure
  mode arriving by accident rather than by design.
- `compute_declaration_points` (`surveys/ratings.py:40`) filters answers by
  `criterion__is_active` only — never by category. It sums whatever criteria the
  subject has answers for. That stays correct as long as subjects are only ever
  *asked* the right questions, which is precisely the guarantee `.first()` does
  not provide.

### Proposed fix

Give `Category` an optional subject type.

```python
# surveys/models.py
subject_type = models.ForeignKey(
    ContentType,
    on_delete=models.PROTECT,
    null=True,
    blank=True,
    related_name="survey_categories",
    help_text="What these questions are about — Candidate, Product, Store. "
    "Null means every subject in the game, which is what Polium relied on "
    "when Candidate was its only rateable thing.",
)
```

Nullable on purpose. Polium's existing behaviour — ask everything in the game —
becomes the explicit meaning of null rather than an accident of there being one
subject, so the migration needs no data backfill and Polium is untouched.

Then one selector, used by every game:

```python
# surveys/service.py
def categories_for(subject: Model, game: str) -> list[Category]:
    """Question sets that apply to this subject.

    A category scoped to this subject's type applies to it. A category with no
    subject type applies to everything in the game — which is what Polium has
    always done, and is now stated rather than implied.
    """
    ct = ContentType.objects.get_for_model(subject)
    return list(
        Category.objects.filter(game=game)
        .filter(Q(subject_type=ct) | Q(subject_type__isnull=True))
        .order_by("pk")
    )


def criteria_for(subject: Model, game: str) -> list[Criterion]:
    return list(
        Criterion.objects.filter(
            category__in=categories_for(subject, game), is_active=True
        )
        .select_related("category")
        .order_by("category__pk", "pk")
    )
```

`spendium/views.py:343` `_rating_ctx` and `polium/views.py:281` `_survey_ctx`
both move onto this. Polium's output is unchanged by construction; Spendium's
becomes correct rather than luckily-ordered.

**Cost:** one migration (nullable FK, no backfill), one new function, two call
sites changed, and the `criteria_version` recorded on a response now needs to
come from *the* category rather than *a* category — see the open question in §6.

---

## 3. What already works for stores

Worth being precise, because it is more than it looks.

`spendium/points.py:55`:

```python
def store_points(purchase: Purchase) -> Decimal:
    """Earned for telling us where you shopped, and for how much."""
    if purchase.store is None:
        return Decimal("0")
    return eligible_spend(purchase) * compute_declaration_points(purchase.store)
```

That is live, and `spendium/test_points.py:105,112,120,144` survey `Store`
objects directly and assert on the payout. So the moment survey responses exist
against a `Store`, **players start earning at that store's rate with no further
work**. The floor (`points.floor_points`) is what they get until then.

Also already present: `Store.generate_sqid()` (`spendium/models.py:40`), the
`store` SQID salt in settings, `Store` on `LifecycleMixin`, and a privacy policy
that already promises store ratings to players
(`templates/spendium/privacy.html:70,100,116`).

What is missing is everything a player can see: no route, no view, no template,
no criteria, no aggregate, no entry point. `templates/spendium/purchase_detail.html:9`
prints `purchase.store.name` as plain text because there is nothing to link to.

---

## 4. What to build

### 4.1 Criteria

New management command `spendium/management/commands/seed_store_criteria.py`,
modelled on `seed_spendium_criteria.py`, creating a category scoped to `Store`:

```python
CATEGORY_NAME = "Store ethics"

OPENING_CRITERIA = [
    ("Does this retailer pay its store staff a living wage?", 100),
    ("Does it treat its suppliers fairly on price and payment terms?", 75),
    ("Does it avoid lobbying against public-interest regulation?", 75),
    ("Does it take responsibility for waste from what it sells?", 50),
    ("Is it honest in its advertising and pricing?", 50),
]
```

Same provisional banner as products. The questions are placeholders — as
`seed_spendium_criteria.py:9` puts it, what the questions *are* is a content
decision and the engine is built to receive whatever the membership later
decides. **These specific five are the weakest part of this plan. Please
overwrite them.**

The weights total 350 ppd for a fully satisfied store, on top of the basket's
own 375. Confirmed as the right magnitude (§6c), and in any case the membership's
to set rather than engineering's.

### 4.2 Aggregation

`spendium/ratings.py` is genuinely product-shaped: merge groups, purchase
counts, `ProductRating`. Rather than copy it, extract the weighted core and have
both call it.

```python
# spendium/ratings.py

@dataclass(frozen=True)
class SubjectRating:
    """Shared shape. ProductRating keeps its name as an alias."""
    score: Decimal | None
    response_count: int
    verified_count: int
    purchase_count: int
    displayable: bool
    publishable: bool

    @property
    def percentage(self) -> int | None: ...


def _weighted_score(response_rows) -> tuple[Decimal | None, int]:
    """The verified/unverified weighted mean, shared by products and stores.

    Lifted verbatim out of `compute` — an unverified response is someone with
    an opinion rather than a receipt, and that discount means the same thing
    whichever subject is being rated.
    """
```

Then the store function:

```python
def compute_store(store: Store) -> SubjectRating:
    """A retailer's rating.

    No merge group: unlike `Product`, `Store` has no `merged_into` and stores
    are deduplicated only by case-insensitive name at `service.py:81`. That is
    a known limit, not an oversight — see §5.
    """
    cutoff = timezone.now() - timedelta(days=RATING_WINDOW_DAYS)
    responses = list(
        SurveyResponse.objects.filter(
            content_type=ContentType.objects.get_for_model(Store),
            object_id=store.pk,
            submitted_at__gte=cutoff,
        ).values_list("pk", "is_verified")
    )
    ...


def store_points_per_dollar(store: Store) -> Decimal:
    """What shopping here is worth. The figure the design wants shown."""
    return compute_declaration_points(store)


def player_has_shopped_at(player: object, store: Store) -> bool:
    """Whether this response is anchored to a receipt.

    Not an eligibility check — anyone signed in may rate any store (§6a). This
    decides `is_verified`, which is what the weighting acts on.

    Only the player-linked layer counts, for the same reason as products: once
    a purchase is anonymised there is deliberately no way to tell whose it was.
    """
    return Purchase.objects.filter(player=player, store=store).exists()
```

**Store publish gate: none.** `compute_store` sets `publishable = displayable`.
For products the purchase-count gate stops a sparse aggregate exposing an
individual basket (`ratings.py:99`); "people who shopped at Loblaws think X"
carries no such risk, and a k-anonymity gate would contradict §6b's decision to
show from the first response. `SubjectRating` keeps the `publishable` field so
products are unaffected and a store gate can be added later if this proves
wrong. Flagging it as a call I made rather than one you gave me.

### 4.3 Surfaces

- Route `spendium/urls.py`: `stores/<str:sqid>/` → `store_detail`, and
  `stores/<str:sqid>/rate/` → `submit_store_survey`. Mirrors the product pair at
  `spendium/urls.py:11-16`.
- `templates/spendium/store_detail.html` + `partials/store_rating_section.html`,
  modelled on `product_rating_section.html`, but leading with **points per
  dollar** and showing the percentage as secondary.
- `_store_rating_ctx` / `submit_store_survey` in `spendium/views.py`, mirroring
  `_rating_ctx` (`:343`) and `submit_product_survey` (`:378`).
- Link the store name at `templates/spendium/purchase_detail.html:9` to the new
  page. This closes half of step 13 in `plans/regression-and-gap-discovery.md:251`.

### 4.4 Trend

`ProductRatingSnapshot` (`spendium/models.py:410`) exists because a rating over a
rolling window cannot be reconstructed later. Exactly the same is true of stores.
Add `StoreRatingSnapshot` with the same shape plus `points_per_dollar` (the
number players are actually choosing on), and extend the daily
`snapshot-product-ratings` task (`spendium/task_views.py:102`) to cover both —
one task, renamed `snapshot-ratings`, rather than a second scheduler entry.

### 4.5 Action Centre

`action_centre.ActionCentre` (`spendium/action_centre.py:40`) gains
`unrated_stores`, alongside `unrated_products` (`:94`), with the same "live
purchases only" restriction. A store the player shopped at and has not rated is
exactly the prompt the design's loop calls for — "the purchase prompts a new
survey, completing the circle" (`design.md:294`).

---

## 5. What I am deliberately not doing

- **Store deduplication and merging.** `service.py:70-82` matches
  case-insensitively on the printed name and creates on a miss, with a comment
  saying so. `Store.flag_count` is hard-coded to `0` (`models.py:44`), so its
  `LifecycleMixin` deprecation is inert. There is no `merged_into` and no
  `merge_group_ids` equivalent. **Rating stores makes this worse in a way it is
  not today**: "LOBLAWS" and "LOBLAWS #1234" will accumulate separate ratings and
  pay different points per dollar for the same chain. I am not fixing it here
  because it is a substantial piece of work with its own design questions, but it
  is the natural next plan and I will add it to `plans/operational-debt.md` in
  the same turn as this document.
- **The audit system** (`design.md:349`) — corporation-initiated locked scores.
  Separate feature.
- **Manufacturer rating surfaces.** `manufacturer_rating` (`ratings.py:177`)
  documents itself as too query-heavy for a view. Unchanged here.
- **Humanium.** As asked.
- **QR codes, brand search, brand suggestion.** Still unbuilt
  (`local_only/todo.md:76`), still out of scope.

---

## 6. Decisions taken

*Answered by Korey, 2026-07-26. Recorded here with what each one changes.*

### a. Anyone may rate anything. The product restriction is a bug.

Not a store question after all — a correction to what was built. `design.md:341`
says anyone may submit a response, players and non-players alike, and that the
manipulation risk is known and accepted. Products do not honour that:
`spendium/views.py:346` gates `can_rate` on `player_has_bought`, and
`product_rating_section.html:86` tells the player "You can rate this once you
have bought it and uploaded the receipt."

**So this plan now fixes products as well as adding stores.** The change:

```python
# spendium/views.py — _rating_ctx
can_rate = player.is_authenticated
# A purchase no longer decides whether you may rate. It decides whether the
# response is anchored to evidence — which is what `is_verified` has always
# meant, and what the weighting at ratings.py:140 already acts on.
will_be_verified = player.is_authenticated and ratings.player_has_bought(player, product)
```

`submit_product_survey` (`views.py:401`) already passes
`is_verified=player_has_bought(...)` and needs no change — it was always
recording the right thing. Only the gate was wrong.

What still limits fraud, none of it purchase-based:

- **One response per player per subject.** `submit_survey` (`surveys/service.py:71`)
  updates the player's existing response rather than adding a second, so a
  subject cannot be voted up by one account repeatedly.
- **A 30-day cooldown** on changing it (`SurveyConfig.cooldown_days`).
- **Unverified responses weigh 0.4** (`UNVERIFIED_RATING_WEIGHT`), so an opinion
  from someone with no receipt moves the score less than one from someone with.
- **Points are gated separately** — see (b). Manipulating a displayed rating
  buys attention, not payouts.

### b. Show from one response. Pay from k. Both knobs already exist.

Yes, this is possible, and it needs no new mechanism — the two-gate structure is
already built and the second gate is already the anti-fraud one:

| Gate | Where | Default | What it governs |
|---|---|---|---|
| `MatchConfig.min_rating_responses` | `spendium/ratings.py:93` | 0 → falls back to k | whether a rating is **shown** |
| `SurveyConfig.min_survey_threshold` (k) | `surveys/ratings.py:69` | 5 | whether a criterion **pays** |

Set `min_rating_responses = 1` and leave k at 5. A store or product then shows a
rating from the first response, while `compute_declaration_points` continues to
**exclude** any criterion with fewer than 5 responses — excluded entirely, not
treated as 0%. So a single rating is visible and worth exactly zero points.

That is the answer to "without opening ourselves to complete fraud": the thing
worth defrauding is the payout, and the payout is on the k gate, which is
per-criterion and needs five distinct players (one response each, per (a)).
Both knobs are admin-editable by design — `spendium/models.py:706` was written
for exactly this bootstrapping case — so the display gate can be ratcheted up as
players arrive without a deploy and without touching what Polium pays.

**The snag (a) creates.** The display gate currently counts **verified**
responses only (`ratings.py:152`: `verified_count >= threshold`), and
`test_ratings.py:140` asserts that unverified responses can *never* clear it.
That was coherent when only purchasers could rate. Under (a) it is not: a store
that twenty people have rated would show nothing until someone uploads a receipt
for it, which defeats the bootstrapping goal entirely.

**Change the display gate to count all responses:**

```python
enough_responses = score is not None and len(responses) >= threshold
```

The score is already weighted, so unverified responses show up in the number
having counted for less. `test_ratings.py:140` must be rewritten — it encodes
the old rule, and rewriting it is part of this change rather than a casualty of
it. `verified_count` stays on the dataclass and stays on screen, because "23
ratings, 4 from people with receipts" is the honest way to present it.

**Display the count next to the score.** A 100% from one person must not read
like a 100% from two hundred. `product_rating_section.html:21` already prints
the counts; the store partial will do the same, and this matters more once the
threshold is 1.

### c. Weights confirmed. ~36,000 points for a $50 perfect shop is the right magnitude.

And the weights are the membership's to set, not engineering's — if that is what
they decide, that is what they get. The seeded five are placeholders under the
same provisional banner as products. No change to the plan; recorded so the
number does not get re-litigated later by someone reading it cold.

### d. `criteria_version` is written and never read.

My question was badly put — you are right that when two categories apply, every
criterion of both applies, and nothing about that is in doubt. What I was asking
was narrower: `SurveyResponse.criteria_version` is *one integer*
(`surveys/models.py:87`), while each `Category` keeps its *own* counter
(`:12`), so two applicable categories mean two version numbers and one field.

Checking it turned up something worth more than the question. **Nothing reads
the field.** Every write is at `surveys/service.py:94`, `spendium/views.py:402`
and the seeding command; every read is a test asserting it was written. Neither
`compute_rating` nor `compute_declaration_points` filters, groups or partitions
by it. So the promise in its own help text —

> "Responses record the version they were given under, so answers to different
> questions are never averaged together as though they were the same."

— is not implemented. Answers given under version 1 and version 2 are pooled
today, exactly as if the field did not exist.

That makes the multi-category ambiguity harmless *right now*, and makes "which
version do we store" the wrong question. The right one is whether the guarantee
is wanted. **Proposed:** move the version onto the answer, where it is
well-defined regardless of how many categories apply:

```python
# surveys/models.py — CriterionAnswer
criteria_version = models.PositiveIntegerField(
    default=1,
    help_text="The version of this criterion's category when it was answered. "
    "Per answer rather than per response, because a subject may be asked "
    "questions from several categories and each keeps its own version.",
)
```

Each answer knows its criterion, which knows its category, which has a version
at the moment of answering. One field, always unambiguous.

**Scoped out of this plan.** Making the aggregation actually honour the version —
deciding whether old-version answers are dropped, decayed, or shown separately —
is a real design question about what a rating *means* across a question change,
and it is not on the path to rating stores. Logged as debt (item 9) rather than
smuggled in here. What this plan does is stop the field getting *more* wrong:
the per-answer field lands with the `subject_type` migration, so multi-category
subjects record something well-defined from the first day they exist.

---

## 7. Risks

- **The `Category.subject_type` migration touches Polium.** Not its data — the
  field is nullable and null means the old behaviour — but its selector changes.
  `tests/gui/test_game_loop.py` covers the Polium survey path end to end and is
  the guard. It must pass unchanged.
- **Points change the moment criteria are seeded.** Seeding store criteria does
  nothing on its own (`compute_declaration_points` needs k responses per
  criterion), but the first store to clear k starts paying above the floor. That
  is intended, and worth knowing the day it happens.
- **Per the working agreement, guards get verified by watching them fail.** The
  category-scoping test in particular: it must fail against the current
  `.first()` selector before the fix lands, or it has not been shown to test
  anything.

---

## Todo

**Structural — criteria scoping (do first)**

- [x] Add `Category.subject_type` (nullable FK to `ContentType`) + migration
- [x] Write a test that a spendium category scoped to `Store` is not offered for
      a `Product` — confirm it FAILS against the current `.first()` selector.
      **Verified failing:** with the old selector it returns
      `['Store question?']` for a product, which is the bug exactly.
- [x] Add `surveys.service.categories_for` / `criteria_for`
- [x] Move `spendium/views.py:_rating_ctx` onto `criteria_for`
- [x] Move `polium/views.py:_survey_ctx` onto `criteria_for`; assert Polium's
      rendered criteria are unchanged. The *set* comes from `criteria_for`; the
      sort stayed in the view, because Polium groups by category name and
      `groupby` needs its input sorted the way it groups. Moving the ordering
      too would have changed the rendered order — "unchanged by construction"
      was not quite true.
- [x] Move `criteria_version` onto `CriterionAnswer` + migration (§6d); write it
      from the criterion's own category. Aggregation still ignores it — that is
      debt item 9, not this plan. Removed from `SurveyResponse` and dropped from
      `submit_survey`'s signature, so there is one home rather than two.
- [x] Point `seed_spendium_criteria.py` at the `Product` content type — and
      correct an existing row, since a database seeded before this field existed
      has it null, which would offer product questions for stores.
- [x] Run `tests/gui/test_game_loop.py` — must pass unchanged. **Passes.**

**Anyone may rate (§6a, §6b) — fixes products as well as enabling stores**

- [x] `_rating_ctx`: `can_rate = player.is_authenticated`; keep
      `will_be_verified` on `player_has_bought`
- [x] Reword `product_rating_section.html:86` — it currently says a purchase is
      required, which will no longer be true. Now the form renders for anyone
      signed in, with a line saying an unbought rating counts for less; the
      old branch became "sign in to rate".
- [x] Display gate counts all responses, not `verified_count`
      (`spendium/ratings.py:152`)
- [x] Rewrite `test_ratings.py:140` — it asserts the old rule directly. Replace
      with: unverified responses *do* clear the display gate, and *do* weigh less
- [x] Test that a rating is displayable at one response and still pays zero
      points, so "visible" and "effective" are demonstrably separate
- [ ] Set `MatchConfig.min_rating_responses = 1` for bootstrapping; leave
      `SurveyConfig.min_survey_threshold` at 5. Config, not code — **left for
      Korey**, it is an admin change in production, not a code change.

**Store rating**

- [x] `seed_store_criteria.py` — "Store ethics", scoped to `Store`, provisional
- [x] Extract `_weighted_score` + `SubjectRating` from `ratings.compute`.
      `ProductRating` is an alias of `SubjectRating`, so product call sites are
      untouched.
- [x] `ratings.compute_store` (no publish gate — §4.2), `store_points_per_dollar`,
      `player_has_shopped_at`
- [x] Tests: verified/unverified weighting, the display threshold, and that an
      unrated store still pays the floor
- [x] Test that a non-purchaser may rate a store and the response is recorded
      unverified — the §6a rule, stated for stores as well as products
- [x] `StoreRatingSnapshot` model + migration
- [x] Rename the snapshot task to `snapshot-ratings`, cover both subjects,
      update `hf/task_urls.py` and the Cloud Scheduler entry. **The scheduler
      job is renamed, so `terraform apply` will destroy `hf-snapshot-product-ratings`
      and create `hf-snapshot-ratings` — expected, and it must land in the same
      deploy as the code or the old job will 404.**
- [x] Test that a store's ppd survives a rating ageing out of the window

**Surfaces**

- [x] `store_detail` view + route + template, leading with points per dollar
- [x] `store_rating_section.html` partial + `submit_store_survey` endpoint
- [x] Link the store name at `purchase_detail.html:9`
- [x] `action_centre.unrated_stores` + Action Centre template entry
- [x] Test the loop end to end: shop → store appears unrated in the Action
      Centre → rate it → ppd moves → next purchase pays more

**Housekeeping**

- [x] Add store deduplication/merging to `plans/operational-debt.md`, noting
      that rating makes fragmentation cost real points — logged as item 8
- [x] Update `plans/regression-and-gap-discovery.md:251` — step 13 is now
      "being built", not "deferred". Half of it: linking receipt *line items* to
      `product_detail` is still open.
- [x] Log `criteria_version` being written and never read — debt item 9
- [x] `ruff check .` and `ruff format .`

---

## Deployment steps

Three things that are not code and will not happen by pushing:

1. **`python manage.py seed_store_criteria`** in production. Nothing has store
   questions until this runs, and the store page renders with no survey.
2. **`terraform apply`** — the scheduler job is renamed, so this destroys
   `hf-snapshot-product-ratings` and creates `hf-snapshot-ratings`. It must land
   alongside the code; the old job would POST to a URL that no longer exists.
3. **Set `MatchConfig.min_rating_responses = 1`** in admin, if bootstrapping
   from the first response is wanted. Left unset it falls back to
   `SurveyConfig.min_survey_threshold` (5), so nothing shows until five people
   have rated a thing. This is the knob §6b is about and it is deliberately not
   hard-coded.

Also worth knowing: **the first store to clear k starts paying above the floor**,
which is intended but is a live change to what purchases are worth.
