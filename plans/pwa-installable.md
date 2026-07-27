# Make the site installable as a PWA

Written 2026-07-26. Nothing implemented — this is the plan only.

The manifest exists and has never worked. Getting from here to installable is
about four specific missing pieces, one of which has a structural wrinkle worth
understanding before any code is written.

---

## Current state

| Piece | State |
|---|---|
| `/manifest.json` | Served (`hf/views.py:13`), linked from `base.html:26` |
| Manifest icons | **Do not exist.** `static/icons/` contains only `.gitkeep` |
| Icon URLs | **404 in production** — verified against the live site |
| Service worker | **Absent.** Nothing in the repo registers or serves one |
| iOS support | **Absent.** No `apple-touch-icon`, no `mobile-web-app-capable` |
| HTTPS | Fine |
| Core JS | Datastar loads from `cdn.jsdelivr.net` (`base.html:25`) |

The icon 404 has two independent causes, and fixing one will not fix the other:

1. The files were never created.
2. The manifest hardcodes `/static/icons/icon-192.png`, but in production
   `STATIC_URL` is the GCS bucket. `/static/` is not served by anything in prod, so
   even a committed icon would still 404 at that path.

**Consequence:** the site is not installable anywhere today. Chromium refuses
without a valid 192px-or-larger icon *and* a service worker with a fetch handler.
iOS refuses to use a manifest icon at all and wants `apple-touch-icon`. All three
are missing, so no platform offers to install it.

## What each platform actually requires

Worth separating, because they disagree and the iOS half is cheaper.

**Chromium (Android, desktop Chrome/Edge)** — manifest with `name`/`short_name`,
`start_url`, `display`, icons at 192 and 512, over HTTPS, *plus* a registered
service worker with a `fetch` handler. The service worker is the reason this is
not just a manifest fix.

**Safari (iOS/iPadOS)** — no service worker needed. Wants `apple-touch-icon` and
`mobile-web-app-capable`; honours `display: standalone` from the manifest on
16.4+. There is no install prompt — the user chooses Add to Home Screen. So iOS
installability is achievable with icons and meta tags alone, and could ship first.

## The structural wrinkle: where the service worker lives

A service worker controls only its own path and below, and must be same-origin.
Static assets here are served from `storage.googleapis.com`, so **the service
worker cannot be a static file.** Shipped from the CDN it would be cross-origin
(rejected outright) and, even same-origin, a worker at `/static/sw.js` could only
control `/static/*` — useless.

It needs a dedicated Django route at `/sw.js` returning JavaScript with
`Content-Type: application/javascript`, exactly as `/manifest.json` is a view
rather than a file, and for the same reason. That is the one piece of this that
cannot be solved by moving files around.

## Rules the service worker has to follow here

These are the ways a naive worker would break this specific app.

**Never touch SSE.** Datastar drives interactivity over server-sent events. A
fetch handler that caches, clones or buffers a streaming response breaks it, and
the symptom is a page that silently stops updating. Bypass anything requesting
`text/event-stream`, everything non-GET, `/tasks/*` and `/admin/*`.

**Never cache authenticated HTML.** Pages carry CSRF tokens and per-player state.
A cached page served later produces CSRF failures on submit and shows one
player's data shape to whoever opens the app next. Network-first for documents,
with a static offline fallback; cache-first only for versioned assets.

**Version the caches and clean up.** A stale service worker outliving a deploy is
the classic failure, and it looks like a site that will not update no matter how
hard the user reloads. A version constant in the worker, `activate` deleting
non-current caches, and a deliberate decision on `skipWaiting`.

**Offline scope, honestly.** v1 should be: hashed static assets cached, an offline
fallback page, nothing else. Queuing a receipt photo taken with no signal is
genuinely valuable — a shop with bad reception is a real scenario — but it needs
Background Sync and IndexedDB and is its own project. Not v1.

## Decisions to make

**`start_url` is currently `/`,** which is the marketing landing page. Someone who
installed the app should not open it onto a pitch. Candidates: `/spendium/`, or the
profile once `plans/login-redirect-to-profile.md` lands. Decide these together —
both are answering "where does a returning player belong".

**Vendor Datastar.** A PWA whose core JS comes from a third-party CDN is inert
whenever that CDN is unreachable, and a service worker cannot usefully cache an
opaque cross-origin response. Moving it into `static/` also matches what
`CLAUDE.md` already claims the project does — no build pipeline, static JS served
directly — and removes a third-party dependency from every page load. Not strictly
required for installability, so it can be sequenced separately, but it is the
difference between an app that works on a bad connection and one that does not.

**Icon design.** Needs a `maskable` variant as well as `any`, or Android
letterboxes it inside a white circle. Pillow is already a dependency, so a
management command could generate the set from one source image and avoid adding
tooling.

**Colours.** `theme_color` is `#000000` and `background_color` `#ffffff`. The
theme colour tints the status bar in standalone, so it should be a deliberate
match for the site's design rather than the placeholder it currently is.

## Dependency worth knowing

Icons reach production via `collectstatic`, which runs in the `hf-migrate` job —
the same arrangement flagged as items 2 and 3 in `plans/operational-debt.md`. It
does currently work, so this is not blocked, but if the icons mysteriously fail to
appear in the bucket, that is where to look.

## What not to do

- No build pipeline. No bundler, no workbox, no npm.
- Do not cache authenticated HTML, for any performance argument.
- Do not attempt offline upload in v1.
- Do not register the worker from an inline script that cannot be versioned.

---

## Todo steps

**Icons and manifest — unblocks iOS on its own**

1. Create a source icon and generate 192 and 512 PNGs, plus a maskable variant
   with adequate safe-area padding. A management command using Pillow keeps this
   reproducible; committing the PNGs is also acceptable.
2. Fix the manifest to build icon URLs through staticfiles storage rather than
   hardcoding `/static/`, so it resolves to the GCS URL in production and locally
   in dev. This is the actual bug behind the 404.
3. Add `id`, `scope`, and a `purpose: "maskable"` icon entry. Set `theme_color`
   and `background_color` deliberately.
4. Add `apple-touch-icon`, `mobile-web-app-capable` and
   `apple-mobile-web-app-status-bar-style` to `base.html`, plus a `theme-color`
   meta tag.
5. Verify on a real iPhone: Add to Home Screen shows the right icon and name, and
   opens without browser chrome.

**Service worker — unblocks Chromium**

6. Add a `/sw.js` view serving the worker with the correct content type, and a
   test asserting the content type, since a wrong one makes registration fail
   silently.
7. Write the worker: version constant, `install` precaching the offline page and
   nothing else risky, `activate` deleting non-current caches, `fetch` with the
   bypass rules above.
8. Build the offline fallback page. Plain, no Datastar, no authenticated content.
9. Register the worker from `base.html` behind a same-origin check, and decide
   `skipWaiting` versus prompting the user to reload.
10. Test: an SSE request passes through untouched. This is the assertion that
    stops someone "optimising" the fetch handler later and breaking every
    interactive page.
11. Test: no authenticated HTML response is written to a cache.
12. Verify installability with Lighthouse, on Android Chrome and desktop.

**Decisions, alongside other plans**

13. Choose `start_url`, together with `plans/login-redirect-to-profile.md`.
14. Vendor Datastar into `static/` and drop the CDN script tag.

**Then**

15. Add `/manifest.json` and `/sw.js` to the reachability checks in
    `plans/regression-and-gap-discovery.md` — both are load-bearing and neither is
    linked from anywhere a crawler would follow.
16. Consider a "you can install this" hint for players who have uploaded a
    receipt or two, rather than on first visit. Optional, and easy to get wrong.
17. Later, and separately: offline receipt capture with Background Sync.
