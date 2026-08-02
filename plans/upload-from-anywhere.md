# Uploading a receipt from wherever you happen to be

Two things, related only by the fact that both are in the way of the same
thirty-second errand: getting to the upload form, and then hitting the controls
on it.

---

## 1. Getting there

The loop is: leave the shop, open the app, land on the receipt you uploaded last
time, want to upload the next one.

From `purchase_detail` on a phone that is currently:

1. Tap the hamburger — on mobile `.nav-links` is behind `#nav-open`.
2. Tap **Spendium**.
3. Tap **Upload a receipt** on the Spendium home page.
4. Tap the file control.
5. Take the photo, confirm it.
6. Tap **Upload**.

Six interactions, three of them pure navigation to a form that has one field.

### What to build

A floating button, bottom-right, on Spendium pages, wrapping a file input:

```html
<form method="post" action="{% url 'spendium:receipt_upload' %}"
      enctype="multipart/form-data" class="fab-form">
  {% csrf_token %}
  <label class="fab" for="fab-receipt">
    <span aria-hidden="true">＋</span>
    <span class="visually-hidden">Add a receipt</span>
  </label>
  <input type="file" id="fab-receipt" name="receipt" accept="image/*"
         class="visually-hidden" onchange="this.form.submit()">
</form>
```

Tap → camera → confirm → uploaded. Three interactions, none of them navigation.

### Why this needs no new endpoint

`receipt_upload` (`views.py:326`) already does exactly the right thing at both
ends:

- **Success** → `redirect("spendium:purchase_detail", pk=purchase.pk)`
  (`views.py:298`), which is where the player wants to be anyway — looking at
  the receipt they just took.
- **Failure** → re-renders the upload page with the error. A context switch, but
  to the one page that has both the explanation and somewhere to try again.

So the whole feature is a form in the layout pointing at a view that already
exists. Worth noticing before designing something cleverer: an SSE upload path,
an inline error region on every page, and a second entry point into
`accept_upload` would all be new surface for no behaviour that is missing.

### Why not Datastar's file upload

Checked against https://data-star.dev/examples/file_upload, because the rest of
the app is Datastar and a plain form looks inconsistent next to it. It should
stay a plain form, and the reason is a hard limit rather than a preference.

Datastar binds a file input with `data-bind:files`, base64-encodes the contents
into signals, and `@post`s them as a JSON body. No form, no multipart.

Django's `DATA_UPLOAD_MAX_MEMORY_SIZE` is the problem. Its documented behaviour
is that the limit is "calculated against the total request size **excluding any
file upload data**" — a JSON body is not file upload data, so it counts in full.
It is not set anywhere in `hf/settings`, so it is Django's default 2,621,440
bytes.

Base64 is 4/3 the size of the bytes it carries, so that ceiling lands at roughly
**1.9MB of actual photo**. Past it, Django raises `RequestDataTooBig` and returns
400 *before any of our code runs*. Two consequences, both bad:

- `SPENDIUM["MAX_UPLOAD_BYTES"]` is 10MB and would be unreachable, as would the
  "That image is larger than 10MB" message written for it
  (`service.py:181-185`).
- Phone camera photos are routinely 2-5MB, so this is the common case failing,
  and failing as an opaque 400 that reads like a server fault rather than a
  limit.

Two supporting reasons pointing the same way:

- **Memory.** Base64 is +33% on the wire and resident in full at both ends.
  Multipart spools anything past 2.5MB to a temp file via
  `TemporaryFileUploadHandler`, which is already in `FILE_UPLOAD_HANDLERS`. The
  JSON path cannot. On a 1GiB container with one worker, where Pillow decoding a
  10MB image is the spike the sizing was built around, this is the wrong
  direction — see item 5 in `plans/operational-debt.md`.
- **The example's own limit.** It caps at 1 MiB. It is a small-file technique,
  and receipts are not small files.

Raising `DATA_UPLOAD_MAX_MEMORY_SIZE` to ~14MB would make it work. It would also
lift the ceiling on every JSON POST in the app, including every Datastar signal
endpoint. That setting exists to bound per-request memory; spending it on
stylistic consistency is a bad trade.

If inline upload feedback is ever wanted without leaving the page, the route is a
small multipart `fetch` plus an SSE response — not `data-bind:files`.

### Where it renders, and where it must not

Spendium pages only — `base.html` is shared with Polium, and a receipt button on
a ballot page is nonsense.

`request.resolver_match.app_name == "spendium"` is enough to scope it, needs no
view changes, and does not touch the eight Spendium templates individually. The
alternative — an intermediate `templates/spendium/base.html` — is structurally
tidier and is churn in every one of those templates for no behaviour.

### Who sees it

It must not appear to someone who cannot use it, because `receipt_upload`
answers them with a 403 page (`views.py:328-329`). That is a fine answer to a
deliberate visit and a bad one to a button that looked available.

`_may_upload` is members-or-trial and costs about two queries: `_is_member`
short-circuits, and `trial_uploads_used` is "one indexed count per player" by
its own docstring. Cheap per page, but it should not run on Polium pages, which
rules out a context processor — those run everywhere. An inclusion tag called
from inside the `app_name` guard costs nothing where it is not rendered.

**Hidden during an emergency stop.** Decided: the tag also checks
`spending.uploads_paused()` and renders nothing while a stop is on.

I had argued the other way — that the "keep this one and come back" message is
the only thing telling the player it was not their fault. Korey's call goes the
other way and it is the better one for a *button*, because the button is an
invitation. Inviting somebody to photograph a receipt we already know we will
refuse spends their attention and their photo to deliver a message they did not
need to receive. The upload page still exists, still explains itself, and is
still one tap further on for anyone who goes looking.

Note this makes the FAB's visibility depend on a value that changes without the
player doing anything, so it can vanish between page loads mid-session. That is
correct — it mirrors the actual state of the service — but it is the sort of
thing that looks like a bug when reported.

---

## 2. The controls are too small

Both are on `receipt_upload.html`, and both are below every published minimum.

| Control | Now | Rendered height |
|---|---|---|
| File input (`:18-19`) | `font-size:.9rem` only | native, ~28-32px on iOS |
| Upload button (`:20-23`) | `padding:.5rem 1.2rem` | ~33px |

Apple's HIG asks for 44×44pt, Material for 48×48dp. Both miss.

The file input is the harder one and it is why it looks the way it does: browsers
will not let you style the native control's box. The fix is the standard one —
take the input out of the visual flow and drive it from a `<label for>` styled as
a real button:

```css
.upload-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-height: 48px;
  padding: .85rem 1.5rem;
  font-size: 1rem;
  border-radius: 6px;
}
@media (max-width: 480px) { .upload-btn { width: 100%; } }
```

Use the clip/`opacity:0` visually-hidden pattern rather than `display:none` —
a `display:none` input is skipped by keyboard focus in some browsers, so the
control becomes mouse-and-touch only.

If the file input auto-submits, the separate **Upload** button disappears from
the mobile flow entirely, which is one fewer small target rather than one
enlarged one. It should still exist for anyone whose browser does not run the
`onchange`.

### Two details that will bite on a phone

**Safe area.** A `position:fixed` button at `bottom:1rem` sits under the iOS home
indicator in an installed PWA — and this app is installable as of `fae4873`. It
needs `bottom: calc(1rem + env(safe-area-inset-bottom))`.

**`capture="environment"` stays.** Decided: straight to the camera, on the FAB
and on the upload page both. It is the behaviour Korey is already using and
liking, and the flow this whole plan is about — leave the shop, photograph the
receipt in your hand — is exactly the one it optimises.

The cost is that a receipt photographed earlier cannot be picked from the library
on iOS. **Decided: give it its own button** rather than weakening the fast path —
see below.

### Two buttons on the upload page

The camera path keeps `capture` and stays primary. A second control drops the
attribute, which is the whole difference — without it iOS offers the photo
library and the files app.

```html
<form method="post" enctype="multipart/form-data">
  {% csrf_token %}

  <label class="upload-btn upload-btn--primary" for="receipt-camera">
    Take a photo
  </label>
  <input type="file" id="receipt-camera" name="receipt"
         accept="image/*" capture="environment"
         class="visually-hidden" onchange="this.form.submit()">

  <label class="upload-btn upload-btn--quiet" for="receipt-library">
    Choose an existing photo
  </label>
  <input type="file" id="receipt-library" name="receipt"
         accept="image/*"
         class="visually-hidden" onchange="this.form.submit()">

  {# Fallback for anything that does not run the onchange. #}
  <button type="submit" class="upload-btn upload-btn--quiet">Upload</button>
</form>
```

**One form, one field name, two inputs.** Django's `MultiPartParser` skips file
parts with no filename, so the untouched input contributes nothing and
`request.FILES.get("receipt")` finds the one that was used. No view change, and
the single submit button remains a valid fallback for either. If somebody
managed to fill both, DOM order decides — unreachable in practice, since the
first `change` submits the form.

**Quieter means lower contrast, not a smaller target.** Both labels get the same
`min-height: 48px`; only the fill and border differ. Shrinking the secondary
button would reintroduce, in a new place, the exact problem the rest of this
section exists to fix.

The FAB stays camera-only. It is the fast path, and a floating button that opens
a chooser is no longer a fast path.

---

## What I am deliberately not doing

- **A bottom nav bar.** A FAB is one element; a nav bar is a layout rewrite, has
  to earn its space on every page, and duplicates a hamburger that works.
- **Uploading over Datastar signals.** Argued above — it would cap receipts at
  about 1.9MB.
- **Restyling the rest of the app's buttons.** Plenty of others are under 44px —
  the prompt buttons in `disambiguation_section.html` among them. Worth a pass of
  its own; doing it inside this change would bury the two controls that were
  actually complained about.

---

## Todo steps

Done 2026-08-02, except the one that needs a phone.

- [x] Add `static/css/spendium.css` with `.fab`, `.upload-btn` and
      `.visually-hidden`, linked from `base.html`.
- [x] Add `spendium/templatetags/spendium_tags.py` with an inclusion tag that
      renders the FAB partial, or nothing when `may_upload` is false or
      `spending.uploads_paused()` is true.
- [x] Add `templates/spendium/partials/upload_fab.html`.
- [x] Call the tag from `base.html`, guarded on
      `request.resolver_match.app_name == "spendium"` — and also off the upload
      page itself, where it would be a shortcut to the page already on screen.
- [x] Rebuild the controls on `receipt_upload.html`: "Take a photo" (primary,
      `capture`) and "Choose an existing photo" (quiet, no `capture`), both
      label-driven at 48px minimum, with the submit button kept as a fallback.
- [x] Test: the FAB renders on a Spendium page for a player who may upload.
- [x] Test: it does not render on a Polium page. Watch this one fail first — the
      scoping guard is the whole reason the tag is called conditionally, and a
      FAB leaking onto Polium is the most likely way this goes wrong. **Verified
      failing:** with the `app_name` clause removed, the receipt button renders
      on `polium:home`.
- [x] Test: it does not render for a player out of trial uploads who is not a
      member — the case that would otherwise walk them into a 403.
- [x] Test: it does not render while `uploads_paused()` is true, and the upload
      page still does. Split into two, because the interesting line is not
      "stopped" but "stopped long enough" — see below.
- [x] Test: posting a receipt to `receipt_upload` from a FAB-shaped request
      (no `Referer` from the upload page) still redirects to the new purchase.
- [x] Test: a multipart upload larger than `DATA_UPLOAD_MAX_MEMORY_SIZE` but
      under `MAX_UPLOAD_BYTES` is accepted. This is the one that would have
      caught the Datastar-signals approach, and it pins the property that made
      multipart the right choice — otherwise the reasoning above lives only in
      this document.
- [x] Test: a photo submitted through the library input is accepted exactly as
      one from the camera input. Both post `receipt`, so this is really a test
      that the unused input is ignored rather than arriving as an empty file and
      shadowing the real one.
- [ ] **Still outstanding.** Check on a real iPhone that "Choose an existing
      photo" offers the library and "Take a photo" still goes straight to the
      camera. The whole difference is one attribute and it is not visible in any
      test. Also worth confirming the FAB clears the home indicator in the
      installed PWA — `env(safe-area-inset-bottom)` is in the CSS but has never
      been seen working.
- [x] Check the tag adds no queries to Polium pages. **Zero**, measured with
      `CaptureQueriesContext` against `polium:home` and a Spendium page.

### What came up while building it

- **`may_upload` moved to `service.py`.** It was `views._may_upload`, and the
  template tag would have had to import a private helper out of the view layer
  to ask the same question. Two callers answering an upload-permission question
  differently is a button that leads to a 403, so it now has one home, next to
  the trial-counting it depends on. `views` calls it like anything else.
- **The stop test is really two tests.** The FAB keys on `uploads_paused()`, not
  `is_stopped()`, and the gap between them is deliberate: a short stop still
  accepts uploads and queues them invisibly, and only once it outlives the image
  retention window are receipts actually refused. So there is a test that the
  button survives a short stop and another that it vanishes after a long one.
  Keying on `is_stopped` would have made a deliberately invisible outage visible.
- **Two tests could not be written with the test client.** `SimpleUploadedFile`
  refuses an empty filename — correctly — so the empty-file-input case is built
  as a raw multipart body instead, which is exactly what a browser sends for an
  untouched input. And the oversized-photo fixture had to be random noise: a
  patterned image at the same dimensions compresses to a few kilobytes and would
  have sailed under the very limit the test exists to cross, passing while
  proving nothing.

**Decisions — all settled 2026-08-02**

- [x] Keep `capture="environment"` (straight to camera) or drop it? **Keep it**,
      on the FAB and the upload page both.
- [x] Hide the FAB during an emergency stop? **Hide it.** I had leaned the other
      way; the reasoning for hiding is recorded above and is better.
- [x] Should the FAB appear on non-Spendium pages? **No — Spendium only.**
- [x] Datastar's file upload instead of a form? **No.** See the section above:
      base64-in-signals caps receipts at ~1.9MB against Django's default
      `DATA_UPLOAD_MAX_MEMORY_SIZE`.
