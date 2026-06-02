# Phase 14 Plan — Landing Page

Implements the six-section landing page described in design.md §"Landing Page Design", plus the infrastructure it depends on: a Spendium waitlist model, a stub signup entry point, and the official `datastar-py` SDK for SSE responses. No real auth is built here — that is a separate phase.

---

## What We Are Building

Six sections, rendered server-side with Django templates:

1. **Hero** — headline, subheading, primary CTA → signup stub
2. **How It Works** — three-step visual (Survey → Rate → Act)
3. **The Games** — two cards: Polium (live, CTA), Spendium (coming soon, email notify form via Datastar)
4. **Why It Matters** — philosophy text, link to HF about page
5. **Social Proof** — Ostrom quote (pre-launch)
6. **Join** — repeat CTA, no second email capture

Supporting pieces:
- `datastar-py` package — official Python SDK for Datastar SSE responses
- `SpendiumWaitlist` model to store notify-me emails
- Stub `accounts/signup` view and URL (placeholder until proper auth is built)
- Landing CSS in `static/css/landing.css`
- Migration for `SpendiumWaitlist`

---

## 14.0 — Install `datastar-py`

The official `datastar-py` package provides `datastar_py.django` with:
- `DatastarResponse` — a `StreamingHttpResponse` subclass with correct SSE headers
- `@datastar_response` decorator — wraps sync/async generator views, applies `DatastarResponse` automatically
- `SSE.patch_elements(html, selector, mode)` — emits a `datastar-patch-elements` event that morphs a DOM element
- `SSE.patch_signals(dict)` — emits a `datastar-patch-signals` event to update client-side signal values
- `read_signals(request)` — reads the Datastar signal payload from a POST body (JSON)

This replaces any need to write a custom SSE helper. The existing `sse_response()` helper in `core/sse.py` uses a JSON data format that is **not** Datastar-compatible and will be removed in a future cleanup phase — it must not be used for Datastar responses.

```
uv add datastar-py
```

- [x] Run `uv add datastar-py`
- [x] Confirm `datastar-py` appears in `pyproject.toml` under `[project] dependencies`

---

## 14.1 — SpendiumWaitlist model (`spendium/models.py`)

The Spendium notify-me form needs somewhere to store emails. One record per email — unique constraint prevents duplicates.

```python
# spendium/models.py
from django.db import models


class SpendiumWaitlist(models.Model):
    email = models.EmailField(unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Spendium waitlist entry"
        verbose_name_plural = "Spendium waitlist entries"

    def __str__(self) -> str:
        return self.email
```

Register in admin:

```python
# spendium/admin.py
from django.contrib import admin
from .models import SpendiumWaitlist


@admin.register(SpendiumWaitlist)
class SpendiumWaitlistAdmin(admin.ModelAdmin):
    list_display = ["email", "created_at"]
    readonly_fields = ["email", "created_at"]
```

- [x] Add `SpendiumWaitlist` to `spendium/models.py`
- [x] Create `spendium/admin.py` and register `SpendiumWaitlistAdmin`
- [x] Run `uv run python manage.py makemigrations spendium` → generates `spendium/migrations/0001_initial.py`
- [x] Run `uv run python manage.py migrate`
- [x] Run `uv run python manage.py check` — must be clean

---

## 14.2 — Stub signup view (`accounts/views.py` and `accounts/urls.py`)

The landing page CTA needs a destination. Full auth (Google, magic link, passkey) is a future phase. A stub view renders a placeholder page so the CTA is wired and testable.

```python
# accounts/views.py
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render


def signup(request: HttpRequest) -> HttpResponse:
    return render(request, "accounts/signup.html")
```

Add to `accounts/urls.py`:

```python
# accounts/urls.py
from django.contrib.auth import views as auth_views
from django.urls import path
from . import views as accounts_views

app_name = "accounts"

urlpatterns = [
    path("login/", auth_views.LoginView.as_view(), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("signup/", accounts_views.signup, name="signup"),
]
```

Template stub:

```html
<!-- templates/accounts/signup.html -->
{% extends "base.html" %}
{% block title %}Sign up — Human Flourishing{% endblock %}
{% block content %}
<main style="max-width:560px;margin:4rem auto;padding:0 1.5rem;font-family:system-ui,sans-serif">
  <h1 style="font-family:Georgia,serif;font-weight:normal;margin-bottom:1rem">Sign up</h1>
  <p>Full sign-up is coming soon. <a href="{% url 'accounts:login' %}">Log in if you already have an account.</a></p>
</main>
{% endblock %}
```

- [x] Add `signup` view to `accounts/views.py`
- [x] Add `path("signup/", ...)` to `accounts/urls.py`
- [x] Create `templates/accounts/signup.html`

---

## 14.3 — Spendium notify view (`spendium/views.py` and `spendium/urls.py`)

The notify-me form POSTs via Datastar to `/spendium/notify/`. The view uses `@datastar_response` from `datastar_py.django`, which handles streaming the SSE response. It yields `SSE.patch_elements()` to replace the form with a success message.

**CSRF note:** Datastar's `@post` sends `application/json` — not `application/x-www-form-urlencoded` — so Django's standard CSRF middleware won't find the token in the POST body. The view is marked `@csrf_exempt`. This is acceptable for an anonymous email capture endpoint (no account, no sensitive state change). The `X-CSRFToken` header approach could be added later if the policy requires it.

**Signal reading:** Datastar sends the current signal store as a JSON object in the POST body. `read_signals(request)` extracts this into a dict. The form binds the email input to a signal named `email` via `data-bind="email"`, so `signals["email"]` will hold the submitted value.

```python
# spendium/views.py
import json

from django.template.loader import render_to_string
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from datastar_py import ServerSentEventGenerator as SSE
from datastar_py.django import datastar_response

from .models import SpendiumWaitlist


@csrf_exempt
@require_POST
@datastar_response
def notify(request):
    try:
        body = json.loads(request.body or b"{}")
        email = str(body.get("email", "")).strip()
    except (json.JSONDecodeError, ValueError):
        email = ""

    if email:
        SpendiumWaitlist.objects.get_or_create(email=email)

    fragment = render_to_string("spendium/partials/notify_success.html")
    yield SSE.patch_elements(fragment, selector="#notify-form")
```

Success partial — replaces the form element entirely:

```html
<!-- templates/spendium/partials/notify_success.html -->
<div id="notify-form" class="notify-success">
  <p>You're on the list. We'll email you when Spendium launches.</p>
</div>
```

Wire the route:

```python
# spendium/urls.py
from django.urls import path
from . import views

app_name = "spendium"

urlpatterns = [
    path("notify/", views.notify, name="notify"),
]
```

- [x] Create `spendium/views.py` with `notify` view
- [x] Create `templates/spendium/partials/notify_success.html`
- [x] Update `spendium/urls.py` with the notify route

---

## 14.4 — Landing view (`hf/views.py` and `hf/urls.py`)

```python
# hf/views.py — add landing view alongside the existing manifest view
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render


def landing(request: HttpRequest) -> HttpResponse:
    return render(request, "landing.html")
```

Complete `hf/urls.py` after adding the root route:

```python
from django.contrib import admin
from django.urls import include, path
from hf import views as hf_views

urlpatterns = [
    path("", hf_views.landing, name="landing"),
    path("manifest.json", hf_views.manifest, name="manifest"),
    path("admin/", admin.site.urls),
    path("accounts/", include("accounts.urls")),
    path("polium/", include("polium.urls")),
    path("spendium/", include("spendium.urls")),
    path("tasks/", include("hf.task_urls")),
]
```

- [x] Add `landing` view to `hf/views.py`
- [x] Add root `path("", ...)` to `hf/urls.py`

---

## 14.5 — CSS (`static/css/landing.css`)

**Colour palette:**
- Background: `#f8f6f2` (warm cream)
- Headings: `#1a1a2e` (deep navy)
- Body text: `#3a3a4e`
- Muted: `#6a6a7e`
- Accent/CTAs: `#2d5a27` (forest green)
- Accent hover: `#1e3d1b`
- Border: `#dedad4`
- Card background: `#ffffff`

**Typography:**
- Headings: `Georgia, 'Times New Roman', serif` — civic, authoritative
- Body: `system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif`

```css
/* static/css/landing.css */

*, *::before, *::after {
  box-sizing: border-box;
  margin: 0;
  padding: 0;
}

:root {
  --bg: #f8f6f2;
  --navy: #1a1a2e;
  --text: #3a3a4e;
  --muted: #6a6a7e;
  --green: #2d5a27;
  --green-dark: #1e3d1b;
  --border: #dedad4;
  --card-bg: #ffffff;
  --font-serif: Georgia, 'Times New Roman', serif;
  --font-sans: system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  --container: 860px;
}

body {
  background: var(--bg);
  color: var(--text);
  font-family: var(--font-sans);
  font-size: 1.0625rem;
  line-height: 1.7;
}

.container {
  max-width: var(--container);
  margin: 0 auto;
  padding: 0 1.5rem;
}

.btn-primary {
  display: inline-block;
  background: var(--green);
  color: #fff;
  font-family: var(--font-sans);
  font-size: 1rem;
  font-weight: 600;
  padding: 0.75rem 1.75rem;
  border-radius: 3px;
  text-decoration: none;
  border: none;
  cursor: pointer;
  transition: background 0.15s;
}
.btn-primary:hover { background: var(--green-dark); }

section { padding: 5rem 0; }
section + section { border-top: 1px solid var(--border); }

/* Hero */
.hero { padding: 6rem 0 5rem; text-align: center; }
.hero h1 {
  font-family: var(--font-serif);
  font-size: clamp(1.75rem, 4vw, 2.75rem);
  font-weight: normal;
  color: var(--navy);
  max-width: 680px;
  margin: 0 auto 1.25rem;
  line-height: 1.25;
}
.hero p {
  font-size: 1.125rem;
  color: var(--muted);
  max-width: 560px;
  margin: 0 auto 2.5rem;
}

/* How It Works */
.how-it-works h2,
.games h2,
.join h2 {
  font-family: var(--font-serif);
  font-size: 1.75rem;
  font-weight: normal;
  color: var(--navy);
  margin-bottom: 2.5rem;
  text-align: center;
}
.how-it-works h2 { margin-bottom: 3rem; }
.steps {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 2.5rem;
}
.step-number {
  font-family: var(--font-serif);
  font-size: 2.5rem;
  color: var(--border);
  line-height: 1;
  margin-bottom: 0.75rem;
}
.step h3 { font-size: 1.0625rem; font-weight: 700; color: var(--navy); margin-bottom: 0.5rem; }
.step p { color: var(--muted); font-size: 0.9375rem; }
.steps-note { margin-top: 2.5rem; text-align: center; color: var(--muted); font-size: 0.9375rem; }

/* Games */
.game-cards { display: grid; grid-template-columns: 1fr 1fr; gap: 2rem; }
.game-card {
  background: var(--card-bg);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 2rem;
}
.game-card h3 {
  font-family: var(--font-serif);
  font-size: 1.375rem;
  font-weight: normal;
  color: var(--navy);
  margin-bottom: 0.75rem;
}
.game-label {
  display: inline-block;
  font-size: 0.75rem;
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--green);
  border: 1px solid var(--green);
  padding: 0.15rem 0.5rem;
  border-radius: 2px;
  margin-bottom: 1rem;
}
.game-label.coming-soon { color: var(--muted); border-color: var(--border); }
.game-card p { color: var(--text); margin-bottom: 1.5rem; }

/* Notify form */
.notify-form { display: flex; gap: 0.5rem; flex-wrap: wrap; }
.notify-form input[type="email"] {
  flex: 1;
  min-width: 0;
  padding: 0.65rem 0.875rem;
  border: 1px solid var(--border);
  border-radius: 3px;
  font-size: 0.9375rem;
  font-family: var(--font-sans);
  background: var(--bg);
  color: var(--text);
}
.notify-form input[type="email"]:focus { outline: 2px solid var(--green); outline-offset: 1px; }
.notify-form button { white-space: nowrap; font-size: 0.9375rem; padding: 0.65rem 1.25rem; }
.notify-success p { color: var(--green); font-weight: 600; margin: 0; }

/* Why */
.why h2 {
  font-family: var(--font-serif);
  font-size: 1.75rem;
  font-weight: normal;
  color: var(--navy);
  margin-bottom: 1.75rem;
}
.why p { max-width: 660px; margin-bottom: 1.25rem; }
.why p:last-of-type { margin-bottom: 1.75rem; }
.why a, .link-secondary { color: var(--green); }
.why a:hover, .link-secondary:hover { color: var(--green-dark); }

/* Social proof */
.social-proof { text-align: center; }
blockquote {
  max-width: 600px;
  margin: 0 auto;
  font-family: var(--font-serif);
  font-size: 1.25rem;
  font-style: italic;
  color: var(--navy);
  line-height: 1.5;
}
blockquote footer {
  margin-top: 1.25rem;
  font-family: var(--font-sans);
  font-size: 0.875rem;
  font-style: normal;
  color: var(--muted);
}

/* Join */
.join { text-align: center; }
.join-links { display: flex; justify-content: center; align-items: center; gap: 2rem; flex-wrap: wrap; }
.link-secondary { font-size: 0.9375rem; }

/* Responsive */
@media (max-width: 640px) {
  .steps { grid-template-columns: 1fr; gap: 2rem; }
  .game-cards { grid-template-columns: 1fr; }
  .hero h1 { font-size: 1.75rem; }
}
```

- [x] Create `static/css/landing.css`

---

## 14.6 — Update `base.html` to support `extra_head` block

```html
<!-- templates/base.html -->
<!doctype html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{% block title %}Human Flourishing{% endblock %}</title>
  <script type="module" src="https://cdn.jsdelivr.net/npm/@starfederation/datastar@latest/dist/datastar.js"></script>
  <link rel="manifest" href="/manifest.json">
  {% block extra_head %}{% endblock %}
</head>
<body>
  {% block content %}{% endblock %}
</body>
</html>
```

- [x] Add `{% block extra_head %}{% endblock %}` inside `<head>` in `templates/base.html`

---

## 14.7 — Landing template (`templates/landing.html`)

**Datastar form pattern used in Section 3:**

The Spendium notify form uses:
- `data-signals="{email: ''}"` on the form — initialises the `email` signal in Datastar's store
- `data-bind="email"` on the input — two-way binds the input value to the `email` signal
- `data-on-submit.prevent="@post('...')"` on the form — intercepts submit, POSTs signals as JSON to the notify endpoint

When the server responds with `SSE.patch_elements(fragment, selector="#notify-form")`, Datastar morphs the `#notify-form` element to the success partial in place. No page reload.

```html
<!-- templates/landing.html -->
{% extends "base.html" %}
{% load static %}

{% block title %}Human Flourishing — Rate. Act. Earn.{% endblock %}

{% block extra_head %}
<link rel="stylesheet" href="{% static 'css/landing.css' %}">
{% endblock %}

{% block content %}

<!-- Section 1: Hero -->
<section class="hero">
  <div class="container">
    <h1>Rate the ethics of the brands you buy from and the politicians you vote for. Earn points. Build a better commons.</h1>
    <p>Human Flourishing is a real-world game that rewards ethical behaviour and holds corporations and politicians accountable — together.</p>
    <a href="{% url 'accounts:signup' %}" class="btn-primary">Start playing — it's free</a>
  </div>
</section>

<!-- Section 2: How It Works -->
<section class="how-it-works">
  <div class="container">
    <h2>How it works</h2>
    <div class="steps">
      <div class="step">
        <div class="step-number">1</div>
        <h3>Survey</h3>
        <p>Rate a politician or brand against ethical criteria. Anyone can contribute. Every response shapes the rating.</p>
      </div>
      <div class="step">
        <div class="step-number">2</div>
        <h3>Rate</h3>
        <p>Ratings update in real time from community input. The more people survey, the more accurate the picture.</p>
      </div>
      <div class="step">
        <div class="step-number">3</div>
        <h3>Act</h3>
        <p>Vote for the highest-rated candidates. Shop at the most ethical brands. Earn points for every action.</p>
      </div>
    </div>
    <p class="steps-note">You can start anywhere. Browse ratings without an account. Survey a candidate you already know. The game meets you where you are.</p>
  </div>
</section>

<!-- Section 3: The Games -->
<section class="games">
  <div class="container">
    <h2>The games</h2>
    <div class="game-cards">

      <div class="game-card">
        <span class="game-label">Live now</span>
        <h3>Polium — The Voting Game</h3>
        <p>Rate politicians against ethical criteria. Declare your vote. Earn points. Hold elected officials accountable for the commitments they made.</p>
        <a href="{% url 'accounts:signup' %}" class="btn-primary">Play now — it's free</a>
      </div>

      <div class="game-card">
        <span class="game-label coming-soon">Coming soon</span>
        <h3>Spendium — The Purchasing Game</h3>
        <p>Rate brands and products against ethical criteria. Shop ethically. Earn points proportional to the store's ethics rating.</p>
        <form id="notify-form"
              class="notify-form"
              data-signals="{email: ''}"
              data-on-submit.prevent="@post('{% url 'spendium:notify' %}')">
          <input type="email"
                 name="email"
                 data-bind="email"
                 required
                 placeholder="your@email.com"
                 aria-label="Email address for Spendium launch notification">
          <button type="submit" class="btn-primary">Notify me when it launches</button>
        </form>
      </div>

    </div>
  </div>
</section>

<!-- Section 4: Why It Matters -->
<section class="why">
  <div class="container">
    <h2>Why it matters</h2>
    <p>Most of us want to do the right thing. The problem is that self-interest is a powerful force — in individuals, in corporations, and in governments. The tragedy of the commons happens when everyone acting in their own interest destroys something we all share.</p>
    <p>Human Flourishing is designed to defeat that tendency. By rewarding ethical behaviour with points, creating transparent ratings that anyone can contribute to, and building an arms race among corporations and politicians to compete on ethical grounds, we change the incentives.</p>
    <p>This is not a charity. It is a game with real-world consequences — for the brands you buy from, for the politicians you vote for, and for the commons we all share.</p>
    <a href="/about/" class="link-secondary">Learn more about Human Flourishing →</a>
  </div>
</section>

<!-- Section 5: Social Proof -->
<section class="social-proof">
  <div class="container">
    <blockquote>
      "What we have ignored is what citizens can do and the importance of real involvement of the people involved."
      <footer>— Elinor Ostrom, Nobel Laureate in Economics, 2009</footer>
    </blockquote>
  </div>
</section>

<!-- Section 6: Join -->
<section class="join">
  <div class="container">
    <h2>Start playing — it's free</h2>
    <div class="join-links">
      <a href="{% url 'accounts:signup' %}" class="btn-primary">Start playing</a>
      <a href="/about/" class="link-secondary">Learn more about Human Flourishing →</a>
    </div>
  </div>
</section>

{% endblock %}
```

- [x] Create `templates/landing.html`

---

## 14.8 — Run collectstatic

```
uv run python manage.py collectstatic --noinput
```

- [x] Run collectstatic after `landing.css` is in place

---

## 14.9 — Smoke test

- [x] `uv run python manage.py check` — clean
- [x] `uv run pytest -v` — all existing tests pass (no regressions)
- [x] Start server and visit `http://localhost:8000/` — all six sections render
- [x] Click "Start playing" → arrives at the signup stub at `accounts/signup/`
- [x] Enter an email and submit the Spendium notify form → form replaced by success message in place, no page reload
- [x] Submit the same email again → same success message (idempotent)
- [x] Confirm email appears in Django admin under Spendium → Spendium waitlist entries
- [x] Resize to < 640px — steps and game cards stack to one column

---

## Phase 14 complete when

- [x] `datastar-py` in `pyproject.toml` dependencies
- [x] `SpendiumWaitlist` model with `email` (unique) and `created_at`; migration applied
- [x] `accounts/signup/` renders the stub template
- [x] `/` renders `landing.html` with all six sections
- [x] `/spendium/notify/` accepts POST, saves email idempotently via `get_or_create`, returns `SSE.patch_elements()` response
- [x] `base.html` has `{% block extra_head %}`
- [x] `static/css/landing.css` collected and served
- [x] Notify form replaces itself with success message on submit (no page reload)
- [x] `uv run python manage.py check` → clean
- [x] `uv run pytest -v` → all tests pass

---

## Implementation notes

**Why `datastar-py` not a custom helper:** The official SDK uses the correct `datastar-patch-elements` event name and wire format. The custom `sse_response()` helper that already exists in `core/sse.py` uses a JSON data payload that is incompatible with Datastar — it was written before the SDK was evaluated. That helper should not be used for Datastar responses and will be removed in a future cleanup.

**CSRF:** The notify view is `@csrf_exempt` because Datastar POSTs `application/json` and Django's middleware won't find the token in the JSON body. For an anonymous email capture with no privileged state change, this is acceptable. If the policy requires CSRF protection on this endpoint in future, the solution is to read the cookie value in a JS snippet and include it in `data-headers`.

**`data-on-submit.prevent`:** The `.prevent` modifier tells Datastar to call `event.preventDefault()` before submitting, suppressing the browser's native form POST. This is the Datastar v1 modifier syntax (dot-separated). If the installed Datastar version uses a different modifier syntax (e.g. `__prevent`), adjust accordingly.

**`/about/` links:** Sections 4 and 6 link to `/about/` which does not exist yet. These will 404 until the about page is built; that is intentional and acceptable.

**Colour palette:** The specific values above are a proposal — the design leaves palette open. All colours are CSS custom properties in `:root`, so the entire scheme can be changed in one place.
