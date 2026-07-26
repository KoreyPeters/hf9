# Spendium: home page, front page, and navigation

Written 2026-07-26, after Spendium Phases 1–14 shipped to production. The
functionality exists and is deployed; there is no way to find it.

## What exists today

- `/spendium/` is **not routed at all** — it returns 404. Every Spendium page
  lives at a deeper path (`/spendium/receipts/`, `/spendium/actions/`, …) with
  no index above them.
- The front page still labels Spendium **"Coming soon"** and offers an email
  notify form, which is now false advertising for a live feature.
- The nav bar has a **Polium** link and no Spendium link. It does have an
  "Actions" link when signed in, which points at the Spendium action centre
  without ever saying the word Spendium.

## Two findings that shape everything below

Neither is a page-design question, and both will decide whether the page works.

### 1. Receipt scanning is members-only, and nothing sells a membership

`_is_member` gates `/spendium/receipts/upload/`, and a non-member gets a 403
page. But `Membership` rows can only be created in the Django admin — there is
no signup route, no payment flow, no upgrade page, no trial. Searching
`accounts/urls.py` for membership returns nothing.

So if the Spendium home page's primary call to action is "scan a receipt", the
overwhelming majority of visitors will click it and hit a wall with no way past.

**Decided 2026-07-26: a free trial of 10 receipt uploads.** Membership is still
what pays for scanning beyond that, so the original product intent survives, but
nobody meets a wall before they have any reason to care about it. Ten is enough
to cover a few weeks of ordinary shopping — long enough for the loop to prove
itself, short enough to still mean something.

Design notes for the trial:

- Count **uploads accepted**, not receipts successfully read. Counting successes
  would mean a failed extraction silently costs the player nothing, which sounds
  generous until you realise it makes the remaining count unpredictable. Counting
  attempts is the one a player can reason about. A receipt we could not read is
  a bug on our side, and the right remedy is not to charge it — so exclude the
  `STATUS_FAILED` case specifically rather than counting successes.
- The count wants to live on `Player` or be derived from `Purchase`. Derived is
  simpler and cannot drift, and the query is cheap and per-player.
- `upload_members_only.html` currently reads as a hard wall. It needs rewriting
  to say how many uploads are left, and only become a wall at zero.
- The remaining count belongs on the home page and the upload page, not hidden
  until it runs out.

### 2. There is nothing to browse yet, and there will not be for a while

A rating is only publishable at `PUBLISH_K = 10` verified responses, or
`PUBLISH_K_SENSITIVE = 25` for sensitive categories, and verified means
receipt-anchored. The production database has just been migrated and holds zero
products, zero purchases, zero ratings.

So the obvious home page — a grid of ethically-rated products — is an empty grid,
and will stay empty until real volume arrives. **The cold-start state is the
main state, not an edge case**, and the page has to be designed for it rather
than have an empty-state panel bolted on.

What *does* work on day one is the player's own loop: upload a receipt, watch it
be read, answer the prompts, collect points. That works with a database of one.
The page should be built around that and let discovery grow into the space
later.

## Design: `/spendium/`

Polium's home already solves an analogous problem with a state machine
(`anonymous` / `no_follows` / `no_elections` / `populated`), and this should
follow the same shape for consistency.

The key structural decision: **Spendium's home is a workbench, not a shop
window.** Polium's home is organised around external events — elections happen
whether or not you turn up. Spendium has no such calendar. The only thing that
ever happens is what the player did, so the page should open with their own
work.

### The action centre problem

`/spendium/actions/` already exists and already is "everything waiting for you".
The home page must not become a second copy of it. The split I propose:

| | |
|---|---|
| **Home** | Orientation and entry. What this is, how to start, what you have done lately, one door into the queue. |
| **Action centre** | The queue itself. Every outstanding item, in priority order. |

Home links *to* the action centre with a count; it never lists the items.

### State: anonymous

The reader has never signed in and does not know what Spendium is.

1. **What it is**, in one sentence, above the fold — rate the ethics of what you
   buy, get points for buying better.
2. **How it works**, three steps, reusing the Survey → Rate → Act language from
   the front page so the two pages agree.
3. **What we do with your receipt**, stated up front rather than buried: the
   photo is deleted within 24 hours, purchases are anonymised after 30 days.
   This is a genuine differentiator and it answers the first objection a
   thoughtful person has. Link to `/spendium/privacy/`.
4. **Sign up** CTA.

Deliberately no product grid. An empty one advertises that nobody is here.

### State: signed in, no purchases

The reader is convinced enough to have an account and has never uploaded.

1. **Upload your first receipt** as the single dominant action.
2. **What will happen** — it is read automatically, you may be asked to confirm
   a few items, you earn points for the shop and more for the items.
3. **What you will not have to do** — no typing in your shopping, no barcode
   scanning.
4. **How many free uploads are left**, stated plainly. Ten to begin with, and
   saying so up front is what makes it read as a gift rather than a trap
   discovered at the eleventh receipt.

### State: signed in, with purchases

1. **A single line of state**: receipts scanned, points earned from spending.
2. **Waiting for you** — the count from `action_centre.new_item_count`, linking
   to the action centre. Suppressed entirely when zero, rather than showing a
   cheerful empty box.
3. **Recent receipts** — the last three, each linking to its detail page, with
   its processing status. This is the thing a returning player actually wants:
   *did the one I uploaded this morning work?*
4. **Upload another** CTA.
5. **Discovery**, *only if* there is anything publishable to show (below).

### Discovery, when it earns its place

Gate the whole section on there being at least, say, six publishable products.
Below that it is noise and it makes the site look abandoned.

Source it from **`ProductRatingSnapshot`**, not from `ratings.compute`. The
snapshot table already holds a daily score per product with `verified_count`, so
"the best-rated things people are actually buying" is one indexed query with a
`verified_count >= PUBLISH_K` filter joined against `category.is_sensitive` for
the stricter threshold.

**Do not call `ratings.manufacturer_rating` from this page.** It is documented as
O(n) queries per product and unfit for a view until batched.

## Design: front page changes

Minimal and honest. The Spendium card in `templates/landing.html` currently
carries a `coming-soon` label and a notify form.

1. **Change the label** from "Coming soon" to **"Beta"**. Extraction quality
   genuinely improves as the catalogue fills, and setting that expectation costs
   nothing.
2. **Replace the notify form** with a CTA to `/spendium/`, mirroring Polium's
   "Play now — it's free".
3. **Rewrite the card copy.** The current text — "Earn points proportional to
   the store's ethics rating" — describes an earlier design. Points are now
   `Σ(criterion value × probability)`, additive across store and product, with a
   floor so participating always pays something. The card should say what it now
   does: scan a receipt, we read it, you earn for shopping and more for telling
   us what you bought.
4. **Keep `SpendiumWaitlist` and the `notify` view** even though the form goes.
   The list is empty so there is nobody to email, but the model and route cost
   nothing to leave in place and removing them is churn.
5. The hero already says "the brands you buy from and the politicians you vote
   for", so it needs no change.

## Design: navigation

1. **Add a Spendium link** beside Polium in `templates/base.html`, pointing at
   the new `spendium:home`. Both games should appear for signed-out visitors,
   as Polium does now.
2. **Reconsider "Actions".** It is a Spendium-only link sitting outside any
   Spendium grouping, and once there is a Spendium link next to it the
   relationship is unclear. Options: leave it (simplest, and the badge is
   valuable), move it under the Spendium page, or relabel. I lean toward leaving
   it and revisiting when Humanium adds a third game's worth of links — the badge
   is the mechanism that brings people back and it should stay prominent.
3. **Check the mobile toggle** still lays out sensibly with another link, since
   `nav.css` drives a hamburger below a breakpoint.

## Bootstrapping: making ratings visible while testing alone

The question was whether `PUBLISH_K` / `PUBLISH_K_SENSITIVE` can be lowered so
that something visibly happens during solo testing.

**They can, but they are not what is stopping you.** There are two gates and
they are easy to conflate:

| Gate | Formula | Protects |
|---|---|---|
| `displayable` | `verified_count >= SurveyConfig.min_survey_threshold` | Statistical meaning |
| `publishable` | `displayable` **and** `purchase_count >= PUBLISH_K` | k-anonymity |

`product_rating_section.html` renders the percentage on `displayable` alone.
`publishable` only decides whether a "not yet published" note appears beside it.
So lowering `PUBLISH_K` removes a caption and nothing else.

What actually hides the number from a solo tester is
**`SurveyConfig.min_survey_threshold`, default 5**. That lives in the database,
not in settings — `SurveyConfig` is a singleton editable in the Django admin — so
it can be changed on the live site immediately, with no deploy, and ratcheted
back up the same way.

**Recommendation: set `min_survey_threshold` to 1 while bootstrapping, and leave
`PUBLISH_K` at 10 and `PUBLISH_K_SENSITIVE` at 25.** They cost nothing while the
catalogue is empty and they are exactly the settings that should already be
correct when the first real players arrive.

Two caveats:

- **The threshold is shared with Polium.** `surveys/ratings.py` uses the same
  value to decide which criteria are eligible for the points calculation, so
  dropping it to 1 lets a single response start affecting Polium payouts. If
  Polium has meaningful activity, that is a real side effect and the fix is a
  Spendium-specific threshold rather than a global one — small, and listed in the
  todo below.
- **`publishable` is not currently enforced as suppression anywhere.** The
  template comment says aggregates are withheld "so no individual basket can be
  reverse-engineered", but a product bought by one person will display that
  person's rating with the caption "from 1 verified rating". The discovery
  section in this plan is the first surface where the gate is genuinely applied.
  Whether the product detail page should also suppress rather than annotate is a
  separate question, and worth answering before there are real players.

## Decisions taken

Recorded 2026-07-26.

1. **Membership gate** — free trial of 10 uploads, as above.
2. **Front page label** — **"Beta"**, not "Live now". Extraction quality genuinely
   improves as the catalogue fills, and saying so costs nothing.
3. **Waitlist** — nobody is on it. Drop the launch-email step; keep the model and
   the `notify` view, which cost nothing to leave in place.
4. **`/spendium/`** — public, with the anonymous state described above.

## Open questions

1. **Does Polium have enough real survey activity** for a global
   `min_survey_threshold` of 1 to distort its points? If so, do the
   Spendium-specific threshold first.
2. **Should the product detail page suppress an unpublishable rating** rather
   than annotate it? Not urgent while you are the only player; it stops being
   optional as soon as anyone else is.

## Todo

**Bootstrapping — do first, no deploy needed**

- [ ] Set `SurveyConfig.min_survey_threshold` to 1 in the admin, so ratings
      appear while testing solo. Leave `PUBLISH_K` alone.
- [ ] Check whether Polium survey activity makes that global change unwise; if
      so, add a Spendium-specific threshold
      (`SPENDIUM["MIN_RATING_RESPONSES"]`, defaulting to the SurveyConfig value)
      and read it from `spendium/ratings.py` instead.
- [ ] Note somewhere durable that the threshold is deliberately low and must go
      back up. A setting lowered "just for now" is exactly the kind that is still
      wrong two years later.

**The free trial**

- [ ] Derive an upload count per player from `Purchase`, excluding
      `STATUS_FAILED`, and gate `receipt_upload` on `count < 10 or is_member`.
- [ ] Rewrite `upload_members_only.html` to show uploads remaining, becoming a
      wall only at zero.
- [ ] Surface the remaining count on the home and upload pages.

**The pages**

- [ ] Add `path("", views.spendium_home, name="home")` to `spendium/urls.py`.
- [ ] Write `spendium_home` as a state machine mirroring `polium_home`:
      `anonymous` / `no_purchases` / `active`.
- [ ] Build `templates/spendium/home.html` with the three states above.
- [ ] Add a publishable-products query against `ProductRatingSnapshot`, honouring
      `PUBLISH_K` and `PUBLISH_K_SENSITIVE`, with the six-product floor before the
      discovery section renders at all.
- [ ] Update the Spendium card in `templates/landing.html`: label, CTA, copy.
- [ ] Add the Spendium link to the nav in `templates/base.html`.
- [ ] Verify the mobile nav layout with the extra link.
- [ ] Tests: each home state renders for the right visitor; the discovery
      section is absent below the floor; an unpublishable product never appears;
      the front page links to `spendium:home` and no longer says "coming soon".
- [ ] Add `/spendium/` to the audience map in `spendium/test_security.py` — that
      test fails for any route with no declared audience.
- [ ] Add `/spendium/` and the front page to the render tests added in
      `accounts/tests.py`, which exist because a template that could never render
      reached production.
