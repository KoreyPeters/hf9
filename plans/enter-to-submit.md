# Enter should submit

Typing a suggestion into "Or describe it yourself" and pressing Enter does
nothing. You have to reach for the mouse.

Following Datastar's own form handling turns this from a one-line fix into a
slightly larger one that also removes a real bug. Both are described below; the
bug is the reason to prefer the larger change.

---

## Why Enter does nothing

`templates/spendium/partials/disambiguation_section.html:122-131`:

```html
<input type="text" data-bind:free_text placeholder="Or describe it yourself" …>
<button data-on:click="@post('…submit_line_free_text…')" …>Save</button>
```

No `<form>`. Enter submits a text field through *implicit form submission*, a
browser behaviour that needs a form containing a submit button. With neither,
the keypress has nothing to do.

## The bug underneath it

`data-signals` is declared **once**, on the container at line 15:

```html
<div id="disambiguation-section"
     data-signals='{"free_text": "", "chosen_product_id": "", …}'>
```

The prompt loop starts at line 39. So every prompt's text box binds to the same
`$free_text`, and every prompt's dropdown to the same `$chosen_product_id`.

Type into the third box and **all of them fill in**. Pick a product in one
dropdown and every dropdown changes. Each Save posts to its own line's URL, so
the right row is updated — the data is not corrupted — but the interface shows
one answer smeared across every question on the receipt.

This got worse recently and I did it: "show the rest"
(`plans/prompt-queue-escape-hatch.md`) removed the cap of five, so a long
receipt can now display a dozen boxes that all mirror each other.

Datastar's form handling fixes this structurally rather than by patching it,
which is the argument for doing it that way.

## What Datastar actually prescribes

From `https://data-star.dev/examples/form_data`, a real `<form>` plus:

```js
@post('/endpoint', {contentType: 'form'})
```

which, per the docs, will *"look for the closest form, perform validation on it,
and send all form elements within it to the backend"*, and *"No signals are sent
to the backend in this type of request."*

Three consequences, all of them wanted here:

1. **Scope follows the form, not the page.** Each prompt gets its own form, so
   `free_text` means *this* prompt's box. The shared-signal bug cannot be
   expressed.
2. **Native validation runs** before anything is sent — `required`, `type="url"`,
   `maxlength`.
3. **Enter works**, because it is a real form with a submit button.

The server reads `request.POST` instead of `read_signals(request)`.

### It also deletes some plumbing

`_prompts_response` currently patches signals back to empty after every answer:

```python
ServerSentEventGenerator.patch_signals({"free_text": "", "chosen_product_id": ""})
```

That exists only because a signal outlives the element patch. Form fields do
not: the whole `#disambiguation-section` is replaced, so the inputs come back
empty on their own. The patch goes away.

## The catch, and it is a real one

"No signals are sent" includes `prompts_expanded`.

`views._expanded` reads that signal to decide whether to re-render the list
expanded, and it is read on every answer specifically so the list does not
collapse back to five each time somebody answers a prompt. Convert the forms and
that signal stops arriving, and expansion breaks on the first answer.

`test_answering_while_expanded_stays_expanded` covers exactly this, so it will
fail rather than the bug shipping — but it has to be designed for, not
discovered.

**The fix is to stop using a signal for it.** Expansion becomes ordinary request
state:

- the toggle becomes `@get('…/prompts/?expanded=1')` — no signal assignment;
- each form carries `<input type="hidden" name="expanded" value="…">`;
- `_expanded(request)` reads `request.GET` or `request.POST` rather than signals.

That is simpler than what is there now, and it removes the last reason this
section declares signals at all.

## Scope

The same missing-form shape exists in `polium/partials/candidates_section.html`
(name, office, URL, bio) and `elections_section.html` (name, date, URL). Those
would benefit more visibly from native validation, since both have required
fields currently enforced only server-side.

**Deliberately excluded: the search boxes.** `polium/home.html:41`,
`create_form.html:28`, `flag_section.html:20` are bound to
`data-on:input__debounce.300ms` and already search as you type. There is no
submit for Enter to trigger.

`polium/partials/create_form.html` is already a real form with a
`type="submit"` button, so Enter works there today — the counter-example showing
what the others lack.

### Cancel buttons

`candidates_section.html:84` and `elections_section.html:67` have Cancel
buttons. Inside a form a `<button>` with no `type` **defaults to submit**, so
both must become `type="button"` or Cancel starts saving the record it exists to
abandon. Silent, and the markup looks unchanged.

## Testing

None of this is visible to `django.test.Client`, which has no keyboard.
`tests/gui/` already runs Playwright with a `make_logged_in_page` fixture, and
that is where the keypress test belongs.

The shared-signal bug, by contrast, *is* testable there and worth pinning: two
prompts, type into the second, assert the first is still empty. That fails
loudly today.

---

## Todo steps

Done 2026-08-03, Spendium only.

- [x] Confirm `contentType: 'form'` exists in Datastar **v1.0.1**, the version
      pinned in `base.html`. Verified against the bundle itself, not the docs:
      it contains `closest("form")`, `new FormData`, `multipart/form-data`, and
      `if (!A.noValidate && !A.checkValidity()) { A.reportValidity(); return; }`
      — so native validation runs and blocks before anything is sent.
- [x] Move expansion off signals. Landed as `?expanded=` on **every** URL that
      re-renders the section, and no hidden fields at all — a query string is
      part of the request whatever the body contains, so `_expanded` reads
      `request.GET` only and does not care which content type was used. Simpler
      than the hidden-field design this plan first proposed.
- [x] Convert the free-text control to its own `<form>` with
      `data-on:submit__prevent="@post(url, {contentType: 'form'})"`, the input
      named `free_text`, and the button `type="submit"`.
- [x] Convert the candidate dropdown the same way, named `chosen_product_id`.
- [x] Update `submit_line_free_text` and `choose_line_product` to read
      `request.POST`.
- [x] Drop the now-pointless `patch_signals` from `_prompts_response`.
- [x] Playwright: type a description, press Enter, assert the line resolves.
- [x] Playwright: with three prompts showing, typing into one leaves the others
      empty.
- [x] Playwright: Save still works by click.
- [x] Playwright, unplanned: an empty box is refused by the browser rather than
      by the server. `required` costs nothing now that `checkValidity()` runs,
      and it removes a round trip that existed only to say "Description was
      empty".
- [x] `test_answering_while_expanded_stays_expanded` still passes, rewritten
      onto the query string.

### Both new guards were watched failing

- **Enter.** Replacing the `<form>` with a `<div>` and a `data-on:click` button —
  the original design — fails `test_enter_submits_a_suggestion`. The keypress
  has nothing to submit.
- **Mirroring.** Restoring `data-signals` on the container and `data-bind:free_text`
  on the input fails `test_typing_in_one_prompt_leaves_the_others_alone` with
  *"Actual value: Bulk Red Lentils"* on the box that was never touched. That is
  the bug as it existed in production, reproduced.

Worth noting the second mutation was applied with a blanket `sed` that also
rewrote the *candidates* form's closing tag, briefly producing a `<form>` closed
by `</div>`. Caught and repaired before the final run; a reminder that
whole-file substitutions in templates are worth avoiding.

**Not done — Polium**

Out of scope by request. `candidates_section.html` and `elections_section.html`
still have the same missing-form shape, and their Cancel buttons still need
`type="button"` **before** any form wrapper is added, or Cancel starts saving
the record it exists to abandon.

**Decisions — settled**

- [x] Full Datastar form conversion, or the minimal wrapper? **Full conversion.**
- [x] Spendium only, or Polium too? **Spendium only for now.**
