# Bot signups, and the mail we are sending to strangers

Twenty of the twenty-one accounts in production look like automated signups
using a harvested email list. The accounts themselves are junk. The problem is
what each one caused us to send.

---

## What is in the list

The usernames are a red herring — `accounts/utils.py:4` is
`uuid.uuid4().hex[:20]`, so every account is a 20-character hex string by
design. That is why they all look the same and it means nothing.

The email addresses are the evidence:

- **Role addresses at real, unrelated businesses.** `support@buckknives.com`,
  `info@nauticaellemme.it`. Nobody signs a company support desk up for a game.
- **Plausible personal addresses at real corporate domains, across countries.**
  `mario.keim@gedore.com`, `katherine.noelling@zeb.de`,
  `carina.schiller@mgm-tp.com`, `jvaladez@brazeway.com`,
  `kristin.bowman@buffaloconstruction.com`. German, Italian and US firms with
  nothing in common.
- **At least one obviously synthetic.** `cxxqgiwianpq@outlook.com`.

That mixture — scraped corporate contacts plus filler — is what a purchased or
crawled list looks like. It is not what organic growth looks like for an
unlaunched app.

**Two checks would confirm it**, and neither is visible in the admin list as it
stands: whether `date_joined` clusters into bursts, and whether any of them ever
verified an email. See the admin gap below.

## The actual damage: we emailed all of them

`accounts/views.py:112` calls `send_verification_email` immediately on signup,
to whatever address was typed in, with nothing establishing that the person
typing owns it.

So a bot submits `support@buckknives.com` and we send Buck Knives mail from our
domain. Twenty times over, to twenty real organisations.

**And it does not stop at one.** `accounts/views.py:118` schedules
`verify-email-reminder` for seven days later, and `accounts/task_views.py:36`
**re-enqueues itself** every seven days until the account is 30 days old
(`task_views.py:21`). Unverified accounts never break the chain — verification
is the only thing that stops it (`task_views.py:18`), and a stranger who never
signed up will never verify.

That is up to **five emails per bogus signup**: one immediately, then day 7, 14,
21 and 28. Twenty accounts is on the order of a hundred unsolicited messages to
people who never asked for any of them.

The costs, worst first:

1. **We are being used as a spam relay**, and the mail is in our name. Somebody
   at `buckknives.com` receives repeated "verify your email" nags from a service
   they have never heard of.
2. **Sender reputation.** Spam complaints land against the Mailgun sending
   domain. Reputation is slow to build and fast to lose, and the mail that
   matters later — actual verification for actual players — is what pays for it.
3. **The data is polluted.** Twenty fake players in a system whose metrics are
   about convergence and community behaviour.

## Why it was possible

Four things, none of them individually silly:

**No bot protection of any kind.** No captcha, no honeypot, no proof of work —
grep finds nothing. Signup is one unauthenticated POST with an email and a name.

**The rate limit is bypassable.** `accounts/ratelimit.py:8-11` reads
`HTTP_X_FORWARDED_FOR` and takes `split(",")[0]` — the **leftmost** entry. In the
general case that is the value the *client* supplied; trusted proxies append on
the right. A bot that sends its own `X-Forwarded-For` header gets a fresh
five-signups-an-hour bucket per request.

*I have not verified Cloud Run's exact handling of a client-supplied
`X-Forwarded-For`* — some proxies strip it, some append. That check is a todo
below, because the fix differs depending on the answer and I would rather not
guess about a security control.

**The rate limit forgets.** `prod.py:13` is `LocMemCache`, so the counters live
in the process, and `min_instance_count = 0` means the container goes away when
idle. Every cold start is a clean slate. Debt item 1 covers the cache; this is a
consequence of it nobody had listed.

**Signup has no password step.** `password=None` and an immediate `login()`
(`views.py:104`, `:124`) — correct for a magic-link and passkey product, and it
does mean there is no second step where a human is required.

## A separate bug found on the way

Both send sites hardcode `from_email="noreply@humanflourishing.org"`
(`email_verification.py:30`, `task_views.py:32`).

That is a **third domain**. `DEFAULT_FROM_EMAIL` is `noreply@humanflourish.ing`
and `MAILGUN_SENDER_DOMAIN` is `mail.humanflourishing.online`. A From address on
a domain Mailgun is not configured to send for either gets rejected outright or
fails SPF/DKIM alignment and lands in spam.

Worth checking the Mailgun logs before anything else: **these emails may not be
arriving at all.** That would cap the damage above considerably — and it would
also mean real players' verification mail has never worked either, which is a
larger problem wearing a smaller hat.

## The admin cannot show you what you need

`PlayerAdmin` (`accounts/admin.py:8`) extends Django's `UserAdmin` without
overriding `list_display`, so the changelist shows username, email, first name,
last name and staff status. For this app that is four useless columns and one
that matters:

- `first_name` / `last_name` are always blank — the app uses `display_name`.
- `display_name` is required at signup (`views.py:82`) and never displayed here,
  so whatever these bots typed is invisible.
- `email_verified` and `date_joined` are the two fields that would settle
  whether a given row is a real person, and neither is shown or filterable.

---

## What to do

### Stop the bleeding

1. **Delete the twenty accounts.** This also cancels the reminder chain for free:
   the task looks up the player and returns on `Player.DoesNotExist`
   (`task_views.py:14-16`), so the scheduled Cloud Tasks become no-ops. No queue
   surgery needed.
2. **Fix the From address**, or confirm the domain is verified in Mailgun.
3. **Add a honeypot and a form-timing check.** Free, invisible to real users, and
   it stops naive scripted posts — which, given the shape of this list, is
   probably what these are.
4. **Fix the X-Forwarded-For parsing** once the Cloud Run behaviour is known.

### Then

5. **A real bot check on signup.** Cloudflare Turnstile is the usual answer and is
   free; it is also a third-party script on the signup page, which is a privacy
   posture decision for a project that publishes a privacy commitment.
6. **Cap the reminder chain.** Four reminders is aggressive even for a genuine
   player who simply is not interested. Two would be plenty.
7. **Rate limit state that survives a restart** — blocked on debt item 1.
8. **Fix `PlayerAdmin`** so the next time this happens it is visible in one
   glance.

### What I am not proposing

- **Blocking role addresses or free-mail domains.** `support@` at a real company
  is a signal here, not a rule worth enforcing — plenty of legitimate people use
  shared mailboxes, and a blocklist is a maintenance burden that bots route
  around in a day.
- **Requiring verification before login.** It would not have prevented any of
  this: the email is sent *before* any verification could happen. It only adds
  friction for real players.

---

## Todo steps

**Immediate**

- [ ] Check Mailgun logs for delivery of mail from `noreply@humanflourishing.org`
      — delivered, rejected, or bounced. This determines how bad the above is and
      whether real verification mail has ever worked. **Still worth doing**, now
      as history rather than diagnosis: if it was all rejected, no stranger was
      ever bothered, and real players have never been able to verify either.
- [x] Confirm from the admin whether the twenty share `date_joined` bursts and
      whether any verified. **Superseded** — Korey deleted all players.
- [x] Delete the confirmed bot accounts. Done by Korey. The scheduled reminders
      become no-ops on their own (`task_views.py:14-16`), so no queue surgery
      was needed.
- [x] Point both send sites at `DEFAULT_FROM_EMAIL` rather than a hardcoded
      third domain. **This turned out to be the most urgent item on the list:**
      `humanflourishing.org` is not merely the wrong domain, it is a domain the
      project no longer owns. Mail we cannot sign fails SPF/DKIM alignment at
      the receiver, so verification mail was likely never arriving for anybody;
      and if the domain has changed hands, its bounces and replies now belong to
      a stranger. Both sites now pass `from_email=None`, which is
      `DEFAULT_FROM_EMAIL`, with a test pinning it.

**Hardening**

- [ ] Determine empirically whether Cloud Run passes through a client-supplied
      `X-Forwarded-For`. Do not fix the parser before knowing.
- [ ] Fix `check_rate_limit` to use a trustworthy client address.
- [ ] Test: a request with a forged `X-Forwarded-For` cannot reset its own rate
      limit bucket. Watch this fail against the current parser first — if it
      passes as-is, then Cloud Run is stripping the header and the parser was
      never the hole.
- [ ] Add a honeypot field and a minimum form-fill time to signup. **Probably
      unnecessary now** that Turnstile is in front of the form; worth revisiting
      only if bots get through it.
- [ ] Test: a submission with the honeypot filled is rejected, and one that
      arrives implausibly fast is rejected.
- [ ] Cap the reminder chain at two, and test that it stops. **Still open**, and
      still worth doing — four reminders is aggressive for a real player who
      simply is not interested.

**Turnstile — done 2026-08-02**

- [x] `accounts/turnstile.py`: `verify()` plus a system check. Verification
      happens in `signup` *before* the account is created, so a refused
      challenge sends no mail — which is the whole point, since the mail was the
      damage rather than the accounts.
- [x] Fails closed on a missing secret in production, and on an unreachable
      Cloudflare. Both verified by inverting them and watching the tests fail.
      The second matters more than it looks: making a third party unreachable
      from our container is easier than solving a challenge, so "Cloudflare
      timed out" must not mean "come in".
- [x] Skipped when `DEBUG` and no secret is set, so the suite and a local
      runserver need no Cloudflare account and make no network calls. Keyed on
      `DEBUG` specifically so it cannot follow a missing env var into
      production.
- [x] A system check reports missing keys at deploy — confirmed by running
      `manage.py migrate` under `hf.settings.prod`, not merely unit-tested.
      **Originally an `Error`, and that was a mistake — see below.**

### Correction: the check took production down

Shipped as an `Error`-level system check. `migrate` inherits
`requires_system_checks = "__all__"` and runs at container start
(`start.sh:10`), so the Error exited non-zero, uvicorn never started, and Cloud
Run crash-looped revision `hf-app-00034-v98` on 2026-08-02.

Nothing was actually lost — `cloudbuild.yaml` deploys `--no-traffic`, smoke
tests, then cuts over, so traffic stayed on the previous revision throughout.
The pipeline caught it. Configuration drift on an already-live service would
not have been caught by anything.

The error in judgement is worth recording, because it was argued for explicitly
and the argument sounded reasonable: a signup form that silently refuses
everybody is a bad failure, so make it loud. True. But the version chosen was so
loud it stopped the whole service — Polium, receipt processing, every task
endpoint — over a key belonging to one form. **The blast radius of a guard must
not exceed the thing it guards**, and "fail closed" applies to the control, not
to the process hosting it.

Now:

- the check is a `Warning`, so the deploy proceeds and the message is still in
  the build output;
- `verify` still fails closed, so signup refuses rather than admitting bots;
- the `accounts` logger is wired to `mail_admins` in `prod.py`, so the first
  attempted signup turns the misconfiguration into an email — which is real
  detection, unlike a log line, and covers the case the deploy-time check never
  could, namely drift after a successful deploy.

Verified by running `manage.py migrate` under production settings with no keys:
exit 0, migrations applied, `accounts.W001` printed.
- [x] All signup renders go through `_signup_page`, so no error branch can drop
      the widget and leave a form that submits without a token. Tested.
- [x] Terraform: `TURNSTILE_SECRET_KEY` in `secrets.tf`, `turnstile_site_key`
      as a plain variable feeding an env var in `cloud_run.tf`. The site key is
      public — it is rendered into the page — so only the secret is a secret.

**Visibility**

- [ ] Give `PlayerAdmin` a `list_display` of email, `display_name`,
      `email_verified`, `date_joined`, `total_points`, and filters on
      `email_verified` and `date_joined`.

**Decisions — settled 2026-08-02**

- [x] Turnstile on signup, or honeypot and rate limiting only? **Turnstile.**
- [x] Is `humanflourishing.org` a domain you own? **No — formerly owned, no
      longer.** Which makes the hardcoded From worse than a typo.
- [x] Delete the twenty outright? **Deleted, all players.**

---

## Before this can ship

Two manual steps, both Korey's, because neither is mine to do:

1. **Create the Turnstile widget** at Cloudflare for `humanflourish.ing` and
   take both keys.
2. **Put the secret in Secret Manager** as `TURNSTILE_SECRET_KEY`, and the site
   key into `terraform/terraform.tfvars` as `turnstile_site_key`. Terraform
   reads secrets as data sources, so a missing one fails `plan` rather than the
   app — and `terraform apply` needs your hands on it either way.

Until both exist, a deploy will boot and warn, and signup will refuse every
submission. It will not take the service down — that was the first version's
behaviour and it was wrong; see the correction above.

**Also worth a look while you are in Cloudflare:** whether
`humanflourishing.org` still resolves anywhere, and whether any published link
or email footer still points at it.
