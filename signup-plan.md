# Signup Experience Plan

Implements the **Player Signup Experience** section of `local_only/design.md` on top of the
auth infrastructure built in Phase 16.

---

## What Phase 16 Already Covers

| Design requirement | Status |
|---|---|
| Google / Apple social login (allauth) | ✅ wired |
| Magic link via django-sesame | ✅ wired |
| Passkey registration + auth | ✅ wired |
| No password at signup | ✅ `set_unusable_password()` on all paths |
| Display name, email, jurisdiction fields | ✅ Player model + signup form |
| Jurisdiction dropdowns with locale pre-fill | ✅ JS in `signup.html` |
| `email_verified` gates points accrual | ✅ `points/service.py` |
| Google/Apple users auto-verified | ✅ `social_account_added` signal |
| Rate limiting on signup and magic link | ✅ `accounts/ratelimit.py` |
| Unlimited jurisdiction following | ✅ `polium.JurisdictionFollow` model |
| Cloud Tasks infrastructure | ✅ `core.tasks.enqueue()` + `@task()` decorator |

---

## Gaps to Close

### §1 — Magic link login auto-verifies email ✅ COMPLETE

**Design:** *"Clicking the login link is de facto email verification. Magic link users are considered
verified on first login and see no verification banner."*

**Current state:** `sesame.views.LoginView` calls `login()` but does not touch `email_verified`.

**Solution:** Add a `user_logged_in` signal handler in `accounts/signals.py`. When Django's
`login()` is called, it stamps `user.backend` on the user object. Sesame sets
`'sesame.backends.ModelBackend'` as the backend, making detection straightforward.

```python
# accounts/signals.py — add alongside the existing social_account_added handler

from django.contrib.auth.signals import user_logged_in

@receiver(user_logged_in)
def auto_verify_on_magic_link(sender, request, user, **kwargs):
    backend = getattr(user, "backend", "")
    if backend == "sesame.backends.ModelBackend" and not user.email_verified:
        user.email_verified = True
        user.email_verified_at = timezone.now()
        user.save(update_fields=["email_verified", "email_verified_at"])
```

No new models, no migrations. One test to add: log in via magic link → `email_verified` is True.

---

### §2 — Post-signup redirect: go to the app, not the landing page ✅ COMPLETE

**Design:** *"They land directly in the app, ready to play."*

**Current state:** `LOGIN_REDIRECT_URL = "/"` sends everyone to the landing page.

**Solution:** Two-part change:

**Part A — Polium needs a home/browse view.**
There is currently no list view in Polium — only detail views (`candidate_detail`,
`election_detail`, `jurisdiction_detail`). The entry point after login should be a page where
the player can immediately start. Add `path("", views.polium_home, name="home")` to
`polium/urls.py`, served at `/polium/`. Change `LOGIN_REDIRECT_URL = "/polium/"` in
`settings/base.py`.

The home view personalises its content based on the player's followed jurisdictions (see §5
for empty states and the jurisdiction prompt). For anonymous visitors it shows upcoming elections
globally, sorted by election date. This makes the redirect sensible for returning users (magic
link re-login, passkey re-auth, Google re-login) as well as new signups.

**Part B — Signup-specific redirect.**
The design distinguishes new signups from returning logins. The signup view (our code — not
sesame/allauth) can redirect to a different URL. Change the final redirect in
`accounts/views.signup` from:

```python
return redirect(settings.LOGIN_REDIRECT_URL)
```

to:

```python
return redirect("accounts:welcome")
```

`accounts:welcome` is a new minimal view (`GET /accounts/welcome/`) that renders a single page:
confirms the account was created, explains the verification email, and has one button — "Start
playing" — that takes the player to `/polium/`. This is not a wizard; it is a single confirmation
screen.

For social and magic link signups (handled by allauth and sesame), `LOGIN_REDIRECT_URL = "/polium/"`
controls the destination — no welcome page needed there, since those paths already imply the
player knows what they are doing.

**Files to change:**
- `polium/views.py` — add `polium_home`
- `polium/urls.py` — add `path("", views.polium_home, name="home")`
- `hf/settings/base.py` — `LOGIN_REDIRECT_URL = "/polium/"`
- `accounts/views.py` — `signup` redirects to `accounts:welcome`; add `welcome` view
- `accounts/urls.py` — add `path("welcome/", views.welcome, name="welcome")`
- `templates/accounts/welcome.html` — new template
- `templates/polium/home.html` — new template (see §5)

---

### §3 — Email verification banner ✅ COMPLETE

**Design:** *"A gentle, persistent but non-intrusive banner: 'Verify your email to start earning
points.' The banner stays until verification is complete. It does not block anything or interrupt
gameplay."*

**Solution:** Add a conditional block at the top of `templates/base.html`, visible only to
authenticated users with `email_verified=False`.

```html
<!-- templates/base.html — inside <body>, above {% block content %} -->
{% if user.is_authenticated and not user.email_verified %}
<div id="verify-banner" style="background:#fffbeb;border-bottom:1px solid #f59e0b;padding:.6rem 1.5rem;font-size:.875rem;text-align:center;color:#78350f">
  Verify your email to start earning points.
  Check your inbox for a link from Human Flourishing.
  <a href="{% url 'accounts:resend_verification' %}" style="margin-left:.75rem;color:#92400e;font-weight:500">Resend</a>
</div>
{% endif %}
```

This requires no JavaScript and no new model — `user.email_verified` is already on the Player.

The banner disappears automatically on the next page load after `email_verified` flips to True
(clicking the link in another tab or on another device). No WebSocket or polling needed.

**Resend link:** Add a `resend_verification` view that calls `send_verification_email()` and
redirects back with a flash message. Rate-limit it using the existing `check_rate_limit`
machinery (3 per hour per IP).

**Files to change:**
- `templates/base.html` — add banner block
- `accounts/urls.py` — add `path("verify-email/resend/", views.resend_verification, name="resend_verification")`
- `accounts/views.py` — add `resend_verification` view

---

### §4 — Weekly verification reminder email via Cloud Tasks ✅ COMPLETE

**Design:** *"A gentle reminder is sent once a week for the first month. After that, no further
reminders. No nagging."*

**Solution:** Use the existing Cloud Tasks infrastructure (`core.tasks.enqueue()` / `@task()`).
Cloud Tasks manages the scheduling state — no new model field or database polling needed.

**Flow:**

1. When signup completes (in `accounts/views.signup`, after the call to `send_verification_email`),
   enqueue a Cloud Task targeting `"verify-email-reminder"` with `{"player_id": player.pk}`,
   scheduled 7 days from now.

2. The same enqueue call is made inside the `social_account_added` signal for players who
   arrive via Google/Apple without a verified email (though in practice this should be rare,
   since social logins are auto-verified — see §1 note).

3. When the task fires, the handler:
   - Loads the player
   - If `player.email_verified`: done — no reschedule
   - If `timezone.now() - player.date_joined > 30 days`: done — no reschedule (reminder window
     expired)
   - Otherwise: send the reminder email, then re-enqueue itself 7 days out

This self-rescheduling pattern means a player who signs up and never verifies receives reminders
at roughly days 7, 14, 21, and 28, then nothing. The 30-day guard on the handler prevents
runaway chains. Cloud Tasks handles delivery reliability, retries, and scheduling — no cron,
no Celery beat.

**`schedule_time` parameter for `enqueue()`:**
`core/tasks.py` currently has no way to schedule a task in the future. One parameter needs to
be added:

```python
# core/tasks.py — updated signature
def enqueue(
    url_path: str,
    payload: dict[str, Any] | None = None,
    schedule_time: datetime | None = None,   # new
) -> None:
    ...
    task_body = {
        "http_request": { ... }
    }
    if schedule_time is not None:
        from google.protobuf import timestamp_pb2
        ts = timestamp_pb2.Timestamp()
        ts.FromDatetime(schedule_time)
        task_body["schedule_time"] = ts
    client.create_task(request={"parent": parent, "task": task_body})
```

In `DEBUG` mode, `enqueue()` already runs the task function directly and synchronously —
a scheduled `DEBUG` call should just return immediately (ignoring `schedule_time`).

**Note:** pytest-django overrides `DEBUG=False` by default. Fixed by adding
`django_debug_mode = "keep"` to `[tool.pytest.ini_options]` in `pyproject.toml`.

**Handler:**

```python
# accounts/task_views.py — new file

from datetime import timedelta
from django.core.mail import send_mail
from django.utils import timezone
from core.tasks import task, enqueue

@task("verify-email-reminder")
def send_verification_reminder(player_id: int) -> None:
    from .models import Player
    try:
        player = Player.objects.get(pk=player_id)
    except Player.DoesNotExist:
        return
    if player.email_verified:
        return
    if timezone.now() - player.date_joined > timedelta(days=30):
        return
    send_mail(
        subject="Reminder: verify your Human Flourishing email",
        message="You still haven't verified your email. Click the link below to start earning points:\n\n"
                "Visit the app to get a new verification link.",
        from_email="noreply@humanflourishing.org",
        recipient_list=[player.email],
    )
    enqueue(
        "verify-email-reminder",
        {"player_id": player_id},
        schedule_time=timezone.now() + timedelta(days=7),
    )
```

**Files to change/add:**
- `core/tasks.py` — add `schedule_time: datetime | None = None` parameter to `enqueue()`
- `accounts/task_views.py` — new file with `send_verification_reminder` Cloud Task handler
- `hf/task_urls.py` — register `verify-email-reminder/`
- `accounts/views.py` — call `enqueue("verify-email-reminder", ...)` at end of `signup`
- `pyproject.toml` — add `django_debug_mode = "keep"` so tests respect dev settings' `DEBUG=True`

---

### §5 — Polium home: jurisdiction following and empty states ✅ COMPLETE

**Design:** *"They land directly in the app, ready to play."* Players can follow as many
jurisdictions as they like.

**Existing infrastructure:** `polium.JurisdictionFollow` already models an unlimited
player→jurisdiction relationship with a `depth` field (`"this"` or `"all"`). The Polium home
view should be built on top of this model — a player's feed is the set of upcoming elections
in their followed jurisdictions (respecting `depth`).

The `jurisdiction_country` and `jurisdiction_region` fields on `Player` are locale hints used
only to pre-fill the signup form dropdowns. They are not the source of truth for the Polium home
— `JurisdictionFollow` is.

**Home view logic:**

```
if anonymous:
    show upcoming elections globally, sorted by election_date ascending
elif player has no followed jurisdictions:
    show jurisdiction discovery prompt (empty state A)
elif player follows jurisdictions but none have upcoming elections:
    show elections from followed jurisdictions (empty, with message) + discover more (empty state B)
else:
    show upcoming elections from followed jurisdictions, sorted by election_date ascending
```

**Empty state A — no followed jurisdictions:**
A prominent prompt: *"Follow jurisdictions to see elections near you."* with a search input
that fuzzy-matches against the `Jurisdiction` table. The player selects one or more jurisdictions
and follows them inline. This is not a redirect — it is the home page itself. Once they follow
at least one, the page refreshes to show their elections.

This replaces the original §5 plan of an `accounts/profile/jurisdiction/` endpoint. Jurisdiction
following is a Polium concern, not an accounts concern.

**Empty state B — followed jurisdictions, no elections:**
Show a message: *"No upcoming elections in your followed jurisdictions."* with a secondary CTA
to browse all jurisdictions and discover more to follow. Display any recent survey activity from
followed jurisdictions as an alternative point of engagement.

**Anonymous state:**
Show the next N upcoming elections globally (across all jurisdictions), sorted by election date,
with the jurisdiction name displayed. Encourage sign-up with a subtle prompt on each election
card: *"Sign in to survey candidates and earn points."*

**Jurisdiction search/follow:**
A `POST /polium/jurisdictions/follow/` endpoint (Polium app) accepts `jurisdiction_id` and
`depth`, creates a `JurisdictionFollow` record, and returns the updated home view fragment
(Datastar SSE response) or redirects. This endpoint is also used when a player clicks "Follow"
on a jurisdiction detail page.

**Files to change/add:**
- `polium/views.py` — add `polium_home`, `follow_jurisdiction`
- `polium/urls.py` — add `path("")` home, `path("jurisdictions/follow/")` and `path("jurisdictions/search/")`
- `templates/polium/home.html` — new template handling all four states (anonymous, empty-A, empty-B, populated)

---

### §6 — Anonymous browsing verification ✅ COMPLETE

**Design:** *"Anyone can browse ratings, candidate profiles, brand profiles, and survey results
without creating an account."*

**Current state:** The Polium detail views (`candidate_detail`, `election_detail`,
`jurisdiction_detail`) do not have `@login_required`. Browsing is already unauthenticated-friendly.

**Action:** Confirm this holds as new views are added. Specifically:

- `polium_home` (§2) must **not** have `@login_required`
- The jurisdiction prompt in `polium_home` must degrade gracefully for anonymous users (show global elections, no follow prompt)
- Survey and vote actions **do** require login — `submit_survey` and `declare_vote` should redirect to `/accounts/login/?next=<current-url>` for unauthenticated users

This is a guard check rather than a build task. Enforce it with a test: an unauthenticated GET
to `/polium/` must return 200, not 302.

---

## File Summary

| File | Change |
|---|---|
| `accounts/signals.py` | Add `auto_verify_on_magic_link` handler |
| `accounts/views.py` | Add `welcome`, `resend_verification`; `signup` redirects to `accounts:welcome`; call `enqueue` at signup |
| `accounts/urls.py` | Add `welcome/`, `verify-email/resend/` |
| `accounts/task_views.py` | New — `send_verification_reminder` Cloud Task handler |
| `core/tasks.py` | Add `schedule_time` parameter to `enqueue()` |
| `hf/task_urls.py` | Register `verify-email-reminder/` |
| `hf/settings/base.py` | `LOGIN_REDIRECT_URL = "/polium/"` |
| `polium/views.py` | Add `polium_home`, `follow_jurisdiction` |
| `polium/urls.py` | Add `path("")` home, `path("jurisdictions/follow/")` |
| `templates/base.html` | Add verification banner block |
| `templates/accounts/welcome.html` | New — post-signup confirmation screen |
| `templates/polium/home.html` | New — home with all four states |
| `pyproject.toml` | Add `django_debug_mode = "keep"` |

---

## Sequencing

1. **§1 — Magic link auto-verification** (signal, no migration, high value, low risk) ✅
2. **§3 — Banner + resend** (base.html change — affects every page, do early) ✅
3. **§2 + §5 — Polium home + follow + welcome page** (must be done together; §2 redirect depends on §5 home existing) ✅
4. **§6 — Anonymous browsing** (audit + test — confirm before Polium home goes live) ✅
5. **§4 — Weekly reminder** (Cloud Tasks — lower urgency; `core/tasks.py` schedule_time change is a prerequisite) ✅

---

## Tests to Add

| Test | File | Status |
|---|---|---|
| Magic link login sets `email_verified=True` | `accounts/tests.py` | ✅ |
| Unverified player sees banner in rendered page | `accounts/tests.py` | ✅ |
| Verified player does not see banner | `accounts/tests.py` | ✅ |
| Resend verification is rate-limited | `accounts/tests.py` | ✅ |
| Signup redirects to `accounts:welcome` | `accounts/tests.py` | ✅ |
| Unauthenticated GET `/polium/` returns 200 | `polium/tests.py` | ✅ |
| Authenticated player with no follows sees empty-state-A | `polium/tests.py` | ✅ |
| Authenticated player with follows + elections sees election list | `polium/tests.py` | ✅ |
| Reminder task skips verified player | `accounts/tests.py` | ✅ |
| Reminder task skips player outside 30-day window | `accounts/tests.py` | ✅ |
| Reminder task re-enqueues for unverified player within window | `accounts/tests.py` | ✅ |
