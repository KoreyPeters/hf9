"""Full game-loop GUI test for Polium."""
import pytest
from playwright.sync_api import Page, expect

from accounts.models import Player
from accounts.utils import generate_username
from surveys.models import Category, Criterion, SurveyConfig


@pytest.mark.django_db
def test_polium_game_loop(live_server, make_logged_in_page):
    # ── Survey setup ─────────────────────────────────────────────────────────
    politician = Category.objects.create(name="Politician", description="", game="polium")
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
    p1.get_by_role("button", name="Declare").click()
    expect(p1.locator("#election-declare-section")).to_contain_text("You declared for John Smith1")

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
    expect(p2.locator("#election-declare-section")).to_contain_text("You declared for John Smith1")

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
    expect(p3.locator("#election-declare-section")).to_contain_text("You declared for John Smith2")

    # ── Step 12: Reload and confirm points unchanged at 100 ───────────────────
    p3.reload()
    expect(p3.locator(".nav-points")).to_have_text("100 pts")

