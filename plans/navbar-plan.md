# Navbar Implementation Plan

## Goal

Add a sticky top navbar to `base.html` that appears on every page. It must work on mobile, display the player's username and point total when authenticated, and link to the main areas of the site.

---

## Design Decisions

**Visual language** — matches the landing page palette: navy `#1a1a2e`, forest green `#2d5a27`, warm off-white background `#f8f6f2`. Georgia serif for the wordmark; system-ui for all other text.

**Position** — `position: sticky; top: 0`. Sticky is preferred over fixed because it keeps the navbar in the document flow — no body padding hacks, no content obscured on first load.

**Mobile toggle** — pure CSS checkbox hack. A hidden `<input type="checkbox">` and a `<label>` hamburger button; `input:checked ~ .nav-links` reveals the full menu. No JavaScript needed; no build pipeline touched.

**User section** — when authenticated: display_label (name + SQID fragment), total_points as an integer, link to their profile, and a sign-out button (POST form, required by Django's LogoutView). When anonymous: Sign in and Sign up links.

**Points formatting** — `{{ user.total_points|floatformat:0 }}` — renders whole number, no decimals.

**Links included**
| Link | Target | Rationale |
|---|---|---|
| `Human Flourishing` (wordmark) | `/` | Always a safe home destination |
| `Polium` | `/polium/` | The active game — primary destination for players |
| `About` | `/about/` | Explains the mission to new visitors |
| Player display label | `accounts:player_profile` with `user.sqid` | Profile page |
| Sign in / Sign up | `accounts:login` / `accounts:signup` | For anonymous visitors |
| Sign out | `accounts:logout` (POST) | Session management |

**What's excluded** — Spendium (not live; linked from the landing page only), admin, and anything game-specific. Keep it short.

---

## Files Changed

| File | Change |
|---|---|
| `static/css/nav.css` | New — all navbar styles |
| `templates/base.html` | Add `{% load static %}`, link `nav.css`, insert `<nav>` HTML above existing content |

No migrations. No views. No URL changes.

---

## Todo Steps

- [ ] Create `static/css/nav.css` with all navbar styles (desktop + mobile breakpoint at 640px)
- [ ] Update `templates/base.html`:
  - Add `{% load static %}` at the top
  - Add `<link rel="stylesheet" href="{% static 'css/nav.css' %}">` in `<head>`
  - Insert `<nav class="site-nav">` block between `<body>` and the email verification banner
- [ ] Verify the navbar renders correctly by running the dev server and checking:
  - Desktop: wordmark left, links centre/right, user section far right
  - Mobile (≤640px): wordmark left, hamburger right, menu expands on tap
  - Authenticated state: display_label + points + profile link + sign out
  - Anonymous state: sign in + sign up

---

## Detailed Navbar HTML Structure

```html
<nav class="site-nav">
  <div class="nav-inner">

    <a href="/" class="nav-wordmark">Human Flourishing</a>

    <!-- Mobile toggle (pure CSS checkbox trick) -->
    <input type="checkbox" id="nav-open" class="nav-toggle-input">
    <label for="nav-open" class="nav-toggle-label" aria-label="Open menu">
      <span></span><span></span><span></span>
    </label>

    <div class="nav-links">
      <a href="{% url 'polium:home' %}" class="nav-link">Polium</a>
      <a href="{% url 'about' %}" class="nav-link">About</a>

      {% if user.is_authenticated %}
        <span class="nav-sep"></span>
        <span class="nav-points">{{ user.total_points|floatformat:0 }} pts</span>
        <a href="{% url 'accounts:player_profile' user.sqid %}" class="nav-link nav-link--user">
          {{ user.display_label }}
        </a>
        <form method="post" action="{% url 'accounts:logout' %}" class="nav-signout">
          {% csrf_token %}
          <button type="submit" class="nav-signout-btn">Sign out</button>
        </form>
      {% else %}
        <span class="nav-sep"></span>
        <a href="{% url 'accounts:login' %}" class="nav-link">Sign in</a>
        <a href="{% url 'accounts:signup' %}" class="nav-link nav-link--cta">Sign up</a>
      {% endif %}
    </div>

  </div>
</nav>
```

---

## CSS Architecture

Defined in `static/css/nav.css`:

```
:root variables (navy, green, bg, border) — scoped to nav, not global
.site-nav — sticky bar, white bg, border-bottom, z-index 100
.nav-inner — flex row, space-between, max-width 1000px, centred
.nav-wordmark — Georgia serif, navy colour
.nav-links — flex row, gap, align-items centre
.nav-link — system-ui, small, navy
.nav-link--cta — green bg, white text, pill shape (the Sign up CTA)
.nav-link--user — muted, slightly smaller
.nav-points — small badge: green text, green border, rounded pill
.nav-sep — thin vertical rule, muted colour
.nav-signout-btn — ghost button, no background
.nav-toggle-input — display:none always
.nav-toggle-label — display:none on desktop, shown on mobile as 3-bar icon

@media (max-width: 640px):
  .nav-toggle-label — display:flex
  .nav-links — display:none by default; position:absolute; top:100%; left:0; right:0;
                flex-direction:column; white bg; border-bottom; padding
  .nav-toggle-input:checked ~ .nav-links — display:flex
  .nav-sep — display:none on mobile (not needed)
```

---

## Notes for Implementation

- The email verification banner currently sits at the top of `<body>`. It will move to just below the `<nav>` — it is not sticky, so it scrolls away with the page content. This is fine: the banner is a low-urgency nudge, not a blocking element.
- The `user.sqid` property is available because `Player` uses `SqidMixin`. Django's `auth` context processor makes `user` available in all templates without any view changes.
- `next` parameter on sign-out is not needed — Django's LogoutView redirects to `LOGOUT_REDIRECT_URL` (check settings) or `/`.
- The wordmark text "Human Flourishing" is long. On very small screens (below ~380px) it may need to truncate. Handle with `overflow:hidden; text-overflow:ellipsis; white-space:nowrap` on `.nav-wordmark`.
