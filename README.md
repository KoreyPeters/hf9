# Human Flourishing (HF)

A suite of real-world games that reward ethical behaviour — ethical spending, ethical voting, ethical life choices. Players earn points for actions that benefit the broader commons, overcoming the natural human tendency toward self-interest.

## The Idea

Most attempts to change consumer or political behaviour rely on moral exhortation. HF takes a different approach: make ethical behaviour feel like winning.

The suite is grounded in the work of **Elinor Ostrom** (Nobel laureate, Economics), who demonstrated that communities *can* sustainably manage shared resources when they develop the right institutional structures — built on trust, monitoring, and collective rule-making. HF applies those principles to everyday life.

## The Three Games

**Spendium** — rewards ethical purchases. Players earn points proportional to the ethics rating of the store they shop at. Ratings are built from community surveys. Businesses that want a credible, locked rating can request a formal HF audit — the primary revenue stream for the organisation.

**Polium** — rewards ethical voting. Players survey political candidates throughout their careers (not just at election time), building a continuously updated community rating. Players declare their vote alignment and earn points. Candidates whose rating falls below 25% are blacklisted; the blacklist follows the person, not the office, and the historical record is permanent.

**Humanium** — rewards ethical life choices. The third game, design deferred until Spendium and Polium are established.

## The Core Loop

All three games share the same mechanic:

1. **Survey → Rating** — players submit yes/no responses about a real-world entity (a store, a candidate), building a continuously updated community ethics rating
2. **Rating → Interest** — ratings are public; high-rated entities attract ethical shoppers and voters
3. **Interest → Action** — players act on ratings (spend, vote, declare), earn points, and are prompted to survey again

Players can enter the loop at any point. The loop is self-reinforcing: more surveys produce more accurate ratings, which drive better actions, which generate more surveys.

## The Organisation

Human Flourishing is governed by four constitutional articles: make the world a wonderful place for everyone forever; foster profound human connections; let evidence win; follow Ostrom's design principles. A Constitutional Court serves as the ultimate arbiter of what is or isn't constitutional — independent of day-to-day operations and robust to changes in leadership.

Membership ($10/year, humans only) confers governance rights. Audit revenue is distributed to members and players in proportion to accumulated points.

## Tech Stack

Django · PostgreSQL · Redis · Datastar (SSE/PWA) · GCP (Cloud Run, Cloud SQL, Cloud Memorystore, Cloud Tasks, Cloud Scheduler, Cloud Storage + CDN) · Mailgun (email)

## License

AGPL v3 — any entity running a modified version of this platform as a network service must release their modifications.