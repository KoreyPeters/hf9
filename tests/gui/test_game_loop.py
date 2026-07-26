"""Full game-loop GUI test for Polium."""

import pytest
from playwright.sync_api import Page, expect

from accounts.models import Player
from accounts.utils import generate_username
from surveys.models import Category, Criterion


@pytest.mark.django_db
def test_polium_game_loop(live_server, make_logged_in_page):
    # ── Survey setup ─────────────────────────────────────────────────────────
    politician = Category.objects.create(
        name="Politician", description="", game="polium"
    )
    Criterion.objects.create(category=politician, question="Criteria one", weight=10)
    Criterion.objects.create(category=politician, question="Criteria two", weight=100)

    # ── Players ───────────────────────────────────────────────────────────────
    player1 = Player.objects.create_user(
        username=generate_username(), email="player1@example.com", password=None
    )
    Player.objects.filter(pk=player1.pk).update(email_verified=True)
    player1.refresh_from_db()

    # ── Step 1: Player 1 logs in and navigates to Polium home ─────────────────
    p1: Page = make_logged_in_page(player1)
    p1.goto(f"{live_server.url}/polium/")
    expect(p1.locator("h1")).to_have_text("Polium")
    # New player has no followed jurisdictions — search box is shown
    expect(p1.locator("#jurisdiction-search")).to_be_visible()

    # ── Step 2: Confirm no elections, candidates, or jurisdictions are listed ──
    expect(p1.locator("a[href*='/polium/elections/']")).to_have_count(0)
    expect(p1.locator("a[href*='/polium/candidates/']")).to_have_count(0)
    expect(p1.locator("a[href*='/polium/jurisdictions/']")).to_have_count(0)

    # ── Step 3: Search "california", confirm no suggestions, create & follow ──
    p1.locator("#jurisdiction-search").fill("California")
    # SSE patches results after 300 ms debounce — wait for the no-results message
    expect(p1.locator("#search-results")).to_contain_text("No jurisdictions found.")
    # No existing jurisdiction follow-buttons in the list
    expect(p1.locator("#search-results button[type='submit']")).to_have_count(0)
    # Open the create form
    p1.get_by_role("button", name='Add "california"').click()
    expect(p1.locator("select[name='level']")).to_be_visible()
    p1.locator("select[name='level']").select_option("province")
    p1.get_by_role("button", name="Add jurisdiction").click()
    # create_jurisdiction auto-follows and redirects back to /polium/
    p1.wait_for_url(f"{live_server.url}/polium/")

    # ── Step 4: Confirm California appears as a followed jurisdiction ──────────
    expect(p1.get_by_role("link", name="California", exact=True)).to_be_visible()

    # ── Step 5: Navigate to California and create "First Election" ────────────
    p1.get_by_role("link", name="California", exact=True).click()
    expect(p1.locator("h1")).to_contain_text("California")
    p1.get_by_role("button", name="+ Add election").click()
    expect(p1.get_by_role("heading", name="Add election")).to_be_visible()
    p1.locator("input[data-bind\\:election_name]").fill("First Election")
    p1.locator("input[data-bind\\:election_date]").fill("2027-11-04")
    p1.get_by_test_id("save-election").click()
    expect(p1.get_by_role("link", name="First Election")).to_be_visible()

    # ── Step 6: Create candidate "John Smith1" running for State Governor ─────
    p1.get_by_role("button", name="+ Add candidate").click()
    expect(p1.get_by_role("heading", name="Add candidate")).to_be_visible()
    p1.locator("input[data-bind\\:candidate_name]").fill("John Smith1")
    p1.locator("input[data-bind\\:candidate_office]").fill("State Governor")
    p1.locator("select[data-bind\\:candidate_election_id]").select_option(index=1)
    p1.get_by_test_id("save-candidate").click()
    expect(p1.get_by_role("link", name="John Smith1")).to_be_visible()

    # ── Step 7: Confirm player has zero points ────────────────────────────────
    expect(p1.locator(".nav-points")).to_have_text("0 pts")

    # ── Step 8: Navigate to John Smith1 and submit a survey ──────────────────
    p1.get_by_role("link", name="John Smith1").click()
    expect(p1.locator("h1")).to_contain_text("John Smith1")

    # Select yes/no by criterion PK — avoids fragile nth() indexing
    crit_one = Criterion.objects.get(question="Criteria one")
    crit_two = Criterion.objects.get(question="Criteria two")
    p1.locator(f"input[name='criterion_{crit_one.pk}'][value='yes']").check()
    p1.locator(f"input[name='criterion_{crit_two.pk}'][value='no']").check()
    p1.get_by_role("button", name="Submit survey").click()
    expect(p1.locator("#survey-section")).to_contain_text("Survey submitted!")

    # ── Step 9: Player earns 100 points (first survey, default config) ────────
    # Nav points are server-rendered — reload to pick up the updated total
    p1.reload()
    expect(p1.locator(".nav-points")).to_have_text("100 pts")

    # ── Step 10: Confirm candidate rating (yes/10 + no/100 = 10/110 ≈ 9%) ────
    expect(p1.locator("main")).to_contain_text("Rating: 9 pts")

    # ── Step 11: Navigate to First Election and declare for John Smith1 ────────
    # The candidate profile has no election link — go via the Polium home,
    # which lists upcoming elections for followed jurisdictions.
    p1.goto(f"{live_server.url}/polium/")
    p1.get_by_role("link", name="First Election").click()
    expect(p1.locator("h1")).to_contain_text("First Election")
    # Only 1 survey recorded — below k-threshold warning must be visible
    expect(p1.locator("#election-declare-section")).to_contain_text(
        "Fewer than 5 surveys recorded"
    )
    p1.get_by_role("button", name="Declare").click()
    expect(p1.locator("#election-declare-section")).to_contain_text(
        "You declared for John Smith1"
    )

    # ── Step 12: Reload and confirm points unchanged at 100 ───────────────────
    # compute_declaration_points returns 0 — no criterion has ≥ 5 responses yet
    # (k-threshold not met), so declaring adds no points at this stage.
    p1.reload()
    expect(p1.locator(".nav-points")).to_have_text("100 pts")

    # ── Players 2 ─────────────────────────────────────────────────────────────
    player2 = Player.objects.create_user(
        username=generate_username(), email="player2@example.com", password=None
    )
    Player.objects.filter(pk=player2.pk).update(email_verified=True)
    player2.refresh_from_db()

    # ── Step 1: Player 2 logs in and navigates to Polium home ─────────────────
    p2: Page = make_logged_in_page(player2)
    p2.goto(f"{live_server.url}/polium/")
    expect(p2.locator("h1")).to_have_text("Polium")
    # New player has no followed jurisdictions — search box is shown
    expect(p2.locator("#jurisdiction-search")).to_be_visible()

    # ── Step 2: Confirm no elections, candidates, or jurisdictions are listed ──
    expect(p2.locator("a[href*='/polium/elections/']")).to_have_count(0)
    expect(p2.locator("a[href*='/polium/candidates/']")).to_have_count(0)
    expect(p2.locator("a[href*='/polium/jurisdictions/']")).to_have_count(0)

    # ── Step 3: Search "california", find existing suggestion, follow it ─────
    p2.locator("#jurisdiction-search").fill("california")
    # SSE patches results — California already exists so a follow button appears
    expect(p2.locator("#search-results")).to_contain_text("California")
    expect(p2.locator("#search-results")).not_to_contain_text("No jurisdictions found.")
    p2.locator("#search-results button[type='submit']").click()
    p2.wait_for_url(f"{live_server.url}/polium/")

    # ── Step 4: Confirm California appears as a followed jurisdiction ──────────
    expect(p2.get_by_role("link", name="California", exact=True)).to_be_visible()

    # ── Step 5: Navigate to California — "First Election" already visible ──────
    p2.get_by_role("link", name="California", exact=True).click()
    expect(p2.locator("h1")).to_contain_text("California")
    # "First Election" is listed before the form is even opened
    expect(p2.get_by_role("link", name="First Election")).to_be_visible()
    # Attempt to add an identical election — same title, same date, same jurisdiction
    p2.get_by_role("button", name="+ Add election").click()
    expect(p2.get_by_role("heading", name="Add election")).to_be_visible()
    p2.locator("input[data-bind\\:election_name]").fill("First Election")
    p2.locator("input[data-bind\\:election_date]").fill("2027-11-04")
    p2.get_by_test_id("save-election").click()
    # Should be rejected — duplicate elections in the same jurisdiction must not be allowed
    expect(p2.locator("#elections-section")).to_contain_text("already exists")

    # ── Step 6: "John Smith1" already visible; duplicate add must be rejected ──
    expect(p2.get_by_role("link", name="John Smith1")).to_be_visible()
    p2.get_by_role("button", name="+ Add candidate").click()
    expect(p2.get_by_role("heading", name="Add candidate")).to_be_visible()
    p2.locator("input[data-bind\\:candidate_name]").fill("John Smith1")
    p2.locator("input[data-bind\\:candidate_office]").fill("State Governor")
    p2.locator("select[data-bind\\:candidate_election_id]").select_option(index=1)
    p2.get_by_test_id("save-candidate").click()
    # Should be rejected — duplicate candidates in the same jurisdiction must not be allowed
    expect(p2.locator("#candidates-section")).to_contain_text("already exists")
    expect(p2.get_by_role("link", name="John Smith1")).to_be_visible()

    # ── Step 7: Confirm player has zero points ────────────────────────────────
    expect(p2.locator(".nav-points")).to_have_text("0 pts")

    # ── Step 8: Navigate to John Smith1 and submit a survey ──────────────────
    p2.get_by_role("link", name="John Smith1").click()
    expect(p2.locator("h1")).to_contain_text("John Smith1")

    # Select yes/no by criterion PK — avoids fragile nth() indexing
    crit_one = Criterion.objects.get(question="Criteria one")
    crit_two = Criterion.objects.get(question="Criteria two")
    p2.locator(f"input[name='criterion_{crit_one.pk}'][value='no']").check()
    p2.locator(f"input[name='criterion_{crit_two.pk}'][value='yes']").check()
    p2.get_by_role("button", name="Submit survey").click()
    expect(p2.locator("#survey-section")).to_contain_text("Survey submitted!")

    # ── Step 9: Player earns 100 points (first survey, default config) ────────
    # Nav points are server-rendered — reload to pick up the updated total
    p2.reload()
    expect(p2.locator(".nav-points")).to_have_text("100 pts")

    # ── Step 10: Confirm candidate rating (yes/10 + no/100 = 10/110 ≈ 9%) ────
    expect(p2.locator("main")).to_contain_text("Rating: 50 pts")

    # ── Step 11: Navigate to First Election and declare for John Smith1 ────────
    p2.goto(f"{live_server.url}/polium/")
    p2.get_by_role("link", name="First Election").click()
    expect(p2.locator("h1")).to_contain_text("First Election")
    p2.get_by_role("button", name="Declare").click()
    expect(p2.locator("#election-declare-section")).to_contain_text(
        "You declared for John Smith1"
    )

    # ── Step 12: Reload and confirm points unchanged at 100 ───────────────────
    # compute_declaration_points returns 0 — no criterion has ≥ 5 responses yet
    # (k-threshold not met), so declaring adds no points at this stage.
    p2.reload()
    expect(p2.locator(".nav-points")).to_have_text("100 pts")

    # ── Player 3 ──────────────────────────────────────────────────────────────
    player3 = Player.objects.create_user(
        username=generate_username(), email="player3@example.com", password=None
    )
    Player.objects.filter(pk=player3.pk).update(email_verified=True)
    player3.refresh_from_db()

    # ── Step 1: Player 3 logs in and navigates to Polium home ─────────────────
    p3: Page = make_logged_in_page(player3)
    p3.goto(f"{live_server.url}/polium/")
    expect(p3.locator("h1")).to_have_text("Polium")
    expect(p3.locator("#jurisdiction-search")).to_be_visible()

    # ── Step 2: Confirm no elections, candidates, or jurisdictions are listed ──
    expect(p3.locator("a[href*='/polium/elections/']")).to_have_count(0)
    expect(p3.locator("a[href*='/polium/candidates/']")).to_have_count(0)
    expect(p3.locator("a[href*='/polium/jurisdictions/']")).to_have_count(0)

    # ── Step 3: Search "california", find existing suggestion, follow it ──────
    p3.locator("#jurisdiction-search").fill("california")
    expect(p3.locator("#search-results")).to_contain_text("California")
    expect(p3.locator("#search-results")).not_to_contain_text("No jurisdictions found.")
    p3.locator("#search-results button[type='submit']").click()
    p3.wait_for_url(f"{live_server.url}/polium/")

    # ── Step 4: Confirm California appears as a followed jurisdiction ──────────
    expect(p3.get_by_role("link", name="California", exact=True)).to_be_visible()

    # ── Step 5: Navigate to California — "First Election" already visible ──────
    p3.get_by_role("link", name="California", exact=True).click()
    expect(p3.locator("h1")).to_contain_text("California")
    expect(p3.get_by_role("link", name="First Election")).to_be_visible()
    p3.get_by_role("button", name="+ Add election").click()
    expect(p3.get_by_role("heading", name="Add election")).to_be_visible()
    p3.locator("input[data-bind\\:election_name]").fill("First Election")
    p3.locator("input[data-bind\\:election_date]").fill("2027-11-04")
    p3.get_by_test_id("save-election").click()
    expect(p3.locator("#elections-section")).to_contain_text("already exists")

    # ── Step 6: "John Smith1" already visible; duplicate rejected; add John Smith2
    expect(p3.get_by_role("link", name="John Smith1")).to_be_visible()
    p3.get_by_role("button", name="+ Add candidate").click()
    expect(p3.get_by_role("heading", name="Add candidate")).to_be_visible()
    p3.locator("input[data-bind\\:candidate_name]").fill("John Smith1")
    p3.locator("input[data-bind\\:candidate_office]").fill("State Governor")
    p3.locator("select[data-bind\\:candidate_election_id]").select_option(index=1)
    p3.get_by_test_id("save-candidate").click()
    expect(p3.locator("#candidates-section")).to_contain_text("already exists")
    p3.locator("input[data-bind\\:candidate_name]").fill("John Smith2")
    p3.locator("input[data-bind\\:candidate_office]").fill("State Governor")
    p3.locator("select[data-bind\\:candidate_election_id]").select_option(index=1)
    p3.get_by_test_id("save-candidate").click()
    expect(p3.get_by_role("link", name="John Smith2")).to_be_visible()

    # ── Step 7: Confirm player has zero points ────────────────────────────────
    expect(p3.locator(".nav-points")).to_have_text("0 pts")

    # ── Step 8: Navigate to John Smith2 and submit a survey ───────────────────
    p3.get_by_role("link", name="John Smith2").click()
    expect(p3.locator("h1")).to_contain_text("John Smith2")
    # No surveys yet — rating must be 0
    expect(p3.locator("main")).to_contain_text("Rating: 0 pts")
    p3.locator(f"input[name='criterion_{crit_one.pk}'][value='no']").check()
    p3.locator(f"input[name='criterion_{crit_two.pk}'][value='yes']").check()
    p3.get_by_role("button", name="Submit survey").click()
    expect(p3.locator("#survey-section")).to_contain_text("Survey submitted!")

    # ── Step 9: Player earns 100 points (first survey, default config) ────────
    p3.reload()
    expect(p3.locator(".nav-points")).to_have_text("100 pts")

    # ── Step 10: Confirm John Smith2 rating (no/10 + yes/100 = 100/110 ≈ 91%) ─
    expect(p3.locator("main")).to_contain_text("Rating: 91 pts")

    # ── Step 11: Navigate to First Election and declare for John Smith2 ────────
    p3.goto(f"{live_server.url}/polium/")
    p3.get_by_role("link", name="First Election").click()
    expect(p3.locator("h1")).to_contain_text("First Election")
    p3.get_by_role("button", name="Declare", exact=True).first.click()
    expect(p3.locator("#election-declare-section")).to_contain_text(
        "You declared for John Smith2"
    )

    # ── Step 12: Reload and confirm points unchanged at 100 ───────────────────
    p3.reload()
    expect(p3.locator(".nav-points")).to_have_text("100 pts")

    # ── Player 3 also surveys John Smith1 (3rd survey of JS1; count now 3) ────
    p3.goto(f"{live_server.url}/polium/")
    p3.get_by_role("link", name="California", exact=True).click()
    p3.get_by_role("link", name="John Smith1").click()
    expect(p3.locator("h1")).to_contain_text("John Smith1")
    p3.locator(f"input[name='criterion_{crit_one.pk}'][value='yes']").check()
    p3.locator(f"input[name='criterion_{crit_two.pk}'][value='no']").check()
    p3.get_by_role("button", name="Submit survey").click()
    expect(p3.locator("#survey-section")).to_contain_text("Survey submitted!")
    p3.reload()
    expect(p3.locator(".nav-points")).to_have_text("200 pts")

    # ── Player 4 ──────────────────────────────────────────────────────────────
    player4 = Player.objects.create_user(
        username=generate_username(), email="player4@example.com", password=None
    )
    Player.objects.filter(pk=player4.pk).update(email_verified=True)
    player4.refresh_from_db()

    p4: Page = make_logged_in_page(player4)
    p4.goto(f"{live_server.url}/polium/")
    expect(p4.locator("#jurisdiction-search")).to_be_visible()
    p4.locator("#jurisdiction-search").fill("california")
    expect(p4.locator("#search-results")).to_contain_text("California")
    p4.locator("#search-results button[type='submit']").click()
    p4.wait_for_url(f"{live_server.url}/polium/")

    # ── Step 8: Survey John Smith1 (4th survey — brings count to 4) ──────────
    p4.get_by_role("link", name="California", exact=True).click()
    p4.get_by_role("link", name="John Smith1").click()
    expect(p4.locator("h1")).to_contain_text("John Smith1")
    p4.locator(f"input[name='criterion_{crit_one.pk}'][value='yes']").check()
    p4.locator(f"input[name='criterion_{crit_two.pk}'][value='no']").check()
    p4.get_by_role("button", name="Submit survey").click()
    expect(p4.locator("#survey-section")).to_contain_text("Survey submitted!")
    p4.reload()
    expect(p4.locator(".nav-points")).to_have_text("100 pts")

    # ── Step 9: Declare for John Smith1, then change to John Smith2 ───────────
    p4.goto(f"{live_server.url}/polium/")
    p4.get_by_role("link", name="First Election").click()
    expect(p4.locator("h1")).to_contain_text("First Election")
    # First declaration — John Smith1 (last in list; JS2 has higher rating so appears first)
    p4.get_by_role("button", name="Declare", exact=True).last.click()
    expect(p4.locator("#election-declare-section")).to_contain_text(
        "You declared for John Smith1"
    )
    # Change declaration to John Smith2 — button now says "Change" since a declaration exists
    p4.get_by_text("Change declaration").click()
    p4.get_by_role("button", name="Change", exact=True).click()
    expect(p4.locator("#election-declare-section")).to_contain_text(
        "You declared for John Smith2"
    )
    p4.reload()
    expect(p4.locator(".nav-points")).to_have_text("100 pts")

    # ── Player 5 ──────────────────────────────────────────────────────────────
    player5 = Player.objects.create_user(
        username=generate_username(), email="player5@example.com", password=None
    )
    Player.objects.filter(pk=player5.pk).update(email_verified=True)
    player5.refresh_from_db()

    p5: Page = make_logged_in_page(player5)
    p5.goto(f"{live_server.url}/polium/")
    p5.locator("#jurisdiction-search").fill("california")
    expect(p5.locator("#search-results")).to_contain_text("California")
    p5.locator("#search-results button[type='submit']").click()
    p5.wait_for_url(f"{live_server.url}/polium/")

    # Before surveying: JS1 has 4 surveys — below k=5, worth 0 pts, warning visible
    p5.get_by_role("link", name="First Election").click()
    expect(p5.locator("#election-declare-section")).to_contain_text(
        "Fewer than 5 surveys recorded"
    )
    expect(p5.locator("#election-declare-section")).to_contain_text("Worth ~0 pts")

    # Survey John Smith1 — 5th survey, hits k-threshold exactly
    p5.goto(f"{live_server.url}/polium/")
    p5.get_by_role("link", name="California", exact=True).click()
    p5.get_by_role("link", name="John Smith1").click()
    expect(p5.locator("h1")).to_contain_text("John Smith1")
    p5.locator(f"input[name='criterion_{crit_one.pk}'][value='yes']").check()
    p5.locator(f"input[name='criterion_{crit_two.pk}'][value='no']").check()
    p5.get_by_role("button", name="Submit survey").click()
    expect(p5.locator("#survey-section")).to_contain_text("Survey submitted!")
    p5.reload()
    expect(p5.locator(".nav-points")).to_have_text("100 pts")

    # After threshold met: JS1 now worth 28 pts (crit1: 4/5×10=8, crit2: 1/5×100=20)
    p5.goto(f"{live_server.url}/polium/")
    p5.get_by_role("link", name="First Election").click()
    expect(p5.locator("#election-declare-section")).to_contain_text("Worth ~28 pts")

    # Declare for John Smith1 — first declaration that earns real points
    p5.get_by_role("button", name="Declare", exact=True).last.click()
    expect(p5.locator("#election-declare-section")).to_contain_text(
        "You declared for John Smith1"
    )
    expect(p5.locator("#election-declare-section")).to_contain_text("+28 points earned")
    p5.reload()
    expect(p5.locator(".nav-points")).to_have_text("128 pts")

    # ── Player 6 ──────────────────────────────────────────────────────────────
    player6 = Player.objects.create_user(
        username=generate_username(), email="player6@example.com", password=None
    )
    Player.objects.filter(pk=player6.pk).update(email_verified=True)
    player6.refresh_from_db()

    p6: Page = make_logged_in_page(player6)
    p6.goto(f"{live_server.url}/polium/")
    p6.locator("#jurisdiction-search").fill("california")
    expect(p6.locator("#search-results")).to_contain_text("California")
    p6.locator("#search-results button[type='submit']").click()
    p6.wait_for_url(f"{live_server.url}/polium/")

    # JS1 already above threshold — confirm real points preview still visible
    p6.get_by_role("link", name="First Election").click()
    expect(p6.locator("#election-declare-section")).to_contain_text("Worth ~28 pts")

    # Survey John Smith1 — 6th survey, further above threshold
    p6.goto(f"{live_server.url}/polium/")
    p6.get_by_role("link", name="California", exact=True).click()
    p6.get_by_role("link", name="John Smith1").click()
    expect(p6.locator("h1")).to_contain_text("John Smith1")
    p6.locator(f"input[name='criterion_{crit_one.pk}'][value='yes']").check()
    p6.locator(f"input[name='criterion_{crit_two.pk}'][value='no']").check()
    p6.get_by_role("button", name="Submit survey").click()
    expect(p6.locator("#survey-section")).to_contain_text("Survey submitted!")
    p6.reload()
    expect(p6.locator(".nav-points")).to_have_text("100 pts")

    # Points preview updated after 6th survey (crit1: 5/6×10=8.33, crit2: 1/6×100=16.67 → 25)
    p6.goto(f"{live_server.url}/polium/")
    p6.get_by_role("link", name="First Election").click()
    expect(p6.locator("#election-declare-section")).to_contain_text("Worth ~25 pts")

    # Declare for John Smith1 — earns 25 pts
    p6.get_by_role("button", name="Declare", exact=True).last.click()
    expect(p6.locator("#election-declare-section")).to_contain_text(
        "You declared for John Smith1"
    )
    expect(p6.locator("#election-declare-section")).to_contain_text("+25 points earned")
    p6.reload()
    expect(p6.locator(".nav-points")).to_have_text("125 pts")

    # Switch from John Smith1 (hero, 25 pts) to John Smith2 (zero, below threshold)
    # Option B: delta = 0 - 25 = -25 pts deducted
    p6.get_by_text("Change declaration").click()
    p6.get_by_role("button", name="Change").click()
    expect(p6.locator("#election-declare-section")).to_contain_text(
        "You declared for John Smith2"
    )
    p6.reload()
    expect(p6.locator(".nav-points")).to_have_text("100 pts")
