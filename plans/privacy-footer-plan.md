# Privacy Policy & Footer Plan

## Goal

Surface the privacy policies at stable URLs, link them from a sitewide footer and the sign-up page, and remove the About link from the navbar (it moves to the footer).

---

## Policy URL Structure

### Options considered

**Option A — One page, both policies on it (`/privacy/`)**
Single page with a Polium section and a Spendium section. Simpler URL structure.
- Problem: the policies grow independently. A user reading the Polium policy has to wade past Spendium content they don't care about, and vice versa. Updating one policy means touching a shared template. Linking from a specific game's page to a relevant anchor (`/privacy/#polium`) is fragile.

**Option B — Two separate pages under each game (`/polium/privacy/` and `/spendium/privacy/`)**
Each game owns its policy page. They can evolve independently. Each policy links naturally from that game's own pages.
- Problem: shared boilerplate (contact details, account data, GDPR rights) is duplicated across both documents.

**Option C — Common page plus game-specific pages (`/privacy/`, `/polium/privacy/`, `/spendium/privacy/`)**
A short `/privacy/` page lists both policies with a one-line description and links out to each. Game-specific pages are self-contained.
- Problem: three pages to maintain; the common page is thin and mostly just a directory.

### Decision: Option B — two separate pages

Rationale:
- The policies differ in what data is collected and how it is used (vote declarations vs. purchase data, political survey responses vs. spending survey responses). These are meaningfully different, not just cosmetically so.
- Each game is linked from its own corner of the app. A candidate profile page should link to the Polium policy; a future Spendium store page should link to the Spendium policy. A shared `/privacy/` URL can't do this cleanly.
- The signup page is game-agnostic today, but the consent line can link to both policies by name — that is more transparent than a generic "Privacy Policy" link that doesn't tell the user which game's practices they're agreeing to.
- Duplication of shared boilerplate (GDPR rights, contact details) is acceptable at this scale. Both policies are drafted from the same source; divergence in the boilerplate sections is unlikely.

### URLs

| Page | URL | Named URL | Template |
|---|---|---|---|
| Polium privacy | `/polium/privacy/` | `polium:privacy` | `templates/polium/privacy.html` |
| Spendium privacy | `/spendium/privacy/` | `spendium:privacy` | `templates/spendium/privacy.html` |

Routing through each game's own `urls.py` is correct — it keeps the policy co-located with the game it covers, and it means the `polium` app owns `/polium/privacy/` just as it owns everything else under `/polium/`.

Spendium's policy is fully drafted (`local_only/Spendium-Privacy-Policy.md`). Both templates render complete documents.

---

## Design Decisions

**Footer placement** — added to `base.html` below `{% block content %}`, so it appears on every page. Minimal: one row of links. Inline styles consistent with the rest of the inner-page styling — no new CSS file.

**About leaves the navbar** — the navbar is for navigation to active game areas. Informational links (About, privacy policies) live in the footer. Removing About from the nav also clears space on small screens.

**Consent line on signup** — a single sentence placed immediately above the "Create account" button, linking to both policies by name. Not a checkbox — consistent with the design doc's principle of minimising friction.

---

## Files Changed

| File | Change |
|---|---|
| `polium/urls.py` | Add `path("privacy/", views.privacy, name="privacy")` |
| `polium/views.py` | Add `privacy` view |
| `templates/polium/privacy.html` | New — Polium privacy policy page |
| `spendium/urls.py` | Add `path("privacy/", views.privacy, name="privacy")` |
| `spendium/views.py` | Add `privacy` view |
| `templates/spendium/privacy.html` | New — Spendium placeholder |
| `templates/base.html` | Remove About nav link; add `<footer>` |
| `templates/accounts/signup.html` | Add consent line above submit button |

---

## Todo Steps

- [ ] Add `privacy` view and URL to `polium/views.py` and `polium/urls.py`
- [ ] Create `templates/polium/privacy.html` from `local_only/Polium-Privacy-Policy.md`
- [ ] Add `privacy` view and URL to `spendium/views.py` and `spendium/urls.py`
- [ ] Create `templates/spendium/privacy.html` (placeholder)
- [ ] Remove About link from `templates/base.html` navbar
- [ ] Add footer to `templates/base.html`
- [ ] Add consent line to `templates/accounts/signup.html`

---

## Detailed Design

### Footer HTML (in `base.html`, after `{% block content %}`)

```html
<footer style="border-top:1px solid #e5e2dc;padding:1.5rem;text-align:center;font-family:system-ui,sans-serif;font-size:.8125rem;color:#9ca3af">
  <a href="{% url 'about' %}" style="color:#9ca3af;text-decoration:none">About</a>
  <span style="margin:0 .5rem">&middot;</span>
  <a href="{% url 'polium:privacy' %}" style="color:#9ca3af;text-decoration:none">Polium Privacy</a>
  <span style="margin:0 .5rem">&middot;</span>
  <a href="{% url 'spendium:privacy' %}" style="color:#9ca3af;text-decoration:none">Spendium Privacy</a>
</footer>
```

### Signup consent line (above the submit button)

```html
<p style="font-size:.8rem;color:#888;margin-bottom:1rem">
  By creating an account you agree to our
  <a href="{% url 'polium:privacy' %}" style="color:#555">Polium</a>
  and
  <a href="{% url 'spendium:privacy' %}" style="color:#555">Spendium</a>
  privacy policies.
</p>
```

### Navbar change

Remove this line from `base.html`:
```html
<a href="{% url 'about' %}" class="nav-link">About</a>
```

The separator (`<span class="nav-sep">`) stays — it still separates the Polium link from the user section.

### Privacy policy page structure (`polium/privacy.html`)

Styled like `about.html`: `max-width:720px`, Georgia serif for headings, system-ui for body, generous line height. Sections from the Markdown source:

- Plain language summary (light-green callout box)
- Numbered sections: Who We Are, What We Collect and Why, How We Use Your Data, etc.
- Effective date and version in the heading area

The `[DATE]` and `[domain]` placeholders from the source document remain as literal visible text — a clear signal they need replacing before launch.

### Spendium privacy page (`spendium/privacy.html`)

Same styling pattern as the Polium policy and `about.html`. Sections from `local_only/Spendium-Privacy-Policy.md`:

- Plain language summary (light-green callout box) — covers purchase data sensitivity, no bank access, receipt deletion, no retailer data sharing
- Numbered sections 1–13: Who We Are, What We Collect and Why (account data, purchase data, receipt photos, QR scan data, gameplay data, location data, technical data), How We Use Your Data, Purchase Data — Heightened Protections, Store Data and Third Parties, Sharing and Disclosure, Data Retention, Your Rights, Security, Cookies and Tracking, Minors, Changes, Contact Us

The heightened protections section (Section 4) is distinctive to Spendium — purchase history sensitivity is explicitly called out. A light-yellow callout box (matching the sensitivity of the content) visually distinguishes it from the standard sections.

The `[DATE]`, `[domain]`, and `[Address]` placeholders remain as visible literal text pending finalisation.
