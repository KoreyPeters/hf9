from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from django.conf import settings
from django.db import models
from django.db.models import Count, Q
from django.utils import timezone
from sqids import Sqids

from core.models import SqidMixin
from lifecycle.models import LifecycleMixin

from .normalisation import normalise_raw_text


class SpendiumWaitlist(models.Model):
    email = models.EmailField(unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Spendium waitlist entry"
        verbose_name_plural = "Spendium waitlist entries"

    def __str__(self) -> str:
        return self.email


class Store(SqidMixin, LifecycleMixin):
    """A retailer, at brand level — one record per chain, not per location.

    Minimal for now. `ProductAlias` scopes aliases by retailer, so this model is
    required before aliases can exist at all. The purchase-side fields
    (participation status, QR identifiers) arrive with the purchase models.
    """

    name = models.CharField(max_length=300, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def generate_sqid(self) -> str:
        return Sqids(alphabet=settings.SQID_SALTS["store"]).encode([self.pk])

    @property
    def flag_count(self) -> int:
        # Community duplicate-flagging for stores is not built yet. Returning 0
        # keeps should_deprecate() inert rather than raising.
        return 0

    def __str__(self) -> str:
        return self.name


class Manufacturer(SqidMixin, LifecycleMixin):
    """The entity ultimately held accountable by a product rating."""

    name = models.CharField(max_length=300, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def generate_sqid(self) -> str:
        return Sqids(alphabet=settings.SQID_SALTS["manufacturer"]).encode([self.pk])

    @property
    def flag_count(self) -> int:
        # As with Store — no flagging mechanism yet.
        return 0

    def __str__(self) -> str:
        return self.name


class ProductCategory(models.Model):
    """Taxonomy for products.

    Distinct from `surveys.Category`, which scopes survey criteria. A product
    belongs to one of these; the criteria asked about it are a separate concern.
    """

    name = models.CharField(max_length=200, unique=True)
    parent = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="children",
    )
    is_sensitive = models.BooleanField(
        default=False,
        help_text="Health, personal care and similar. Raises the k-anonymity bar "
        "before aggregates derived from this category may be published.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name_plural = "product categories"

    def __str__(self) -> str:
        return self.name


class Product(SqidMixin):
    """A canonical product line — the subject a rating attaches to.

    Granularity is the product *line*, not the SKU. Size and packaging variants
    collapse into one record, so `canonical_name` follows
    `[Brand] [Product Name] [Variant]` and deliberately **excludes size**.

    HF criteria measure ethical properties, and a 250ml and a 100ml tube of the
    same toothpaste are ethically identical. Splitting them would divide ratings
    across records so each takes several times as long to clear the display
    threshold, and would force players to disambiguate a size distinction that
    does not affect the rating. Variant is retained — "Bright Whitening" versus
    "Cavity Protection" is a real distinction, and one manufacturers name
    distinctly.
    """

    STATUS_UNVERIFIED = "unverified"
    STATUS_VERIFIED = "verified"
    STATUS_RETIRED = "retired"
    STATUS_CHOICES = [
        (STATUS_UNVERIFIED, "Unverified"),
        (STATUS_VERIFIED, "Verified"),
        (STATUS_RETIRED, "Retired"),
    ]

    SOURCE_GEMINI = "gemini"
    SOURCE_UPC_LOOKUP = "upc_lookup"
    SOURCE_ADMIN = "admin"
    SOURCE_PLAYER = "player"
    SOURCE_CHOICES = [
        (SOURCE_GEMINI, "Gemini interpretation"),
        (SOURCE_UPC_LOOKUP, "UPC / open database lookup"),
        (SOURCE_ADMIN, "Admin"),
        (SOURCE_PLAYER, "Player supplied"),
    ]

    canonical_name = models.CharField(
        max_length=300,
        help_text="[Brand] [Product Name] [Variant] — no size. Size variants "
        "collapse into a single product line.",
    )
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default=STATUS_UNVERIFIED
    )
    merged_into = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="merged_from",
        help_text="Set when this record is retired into another. Ratings and "
        "aliases resolve through this link.",
    )
    manufacturer = models.ForeignKey(
        Manufacturer,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="products",
    )
    category = models.ForeignKey(
        ProductCategory,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="products",
    )
    confidence_source = models.CharField(
        max_length=20, choices=SOURCE_CHOICES, default=SOURCE_GEMINI
    )
    reformulated_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Reserved for the case where a product changes formulation "
        "under the same name. Not yet acted on.",
    )
    HOT_TRENDING = "trending"
    HOT_RATING_MOVED = "rating_moved"
    HOT_MANUAL = "manual"
    HOT_REASON_CHOICES = [
        (HOT_TRENDING, "Being bought unusually often"),
        (HOT_RATING_MOVED, "Rating has moved sharply"),
        (HOT_MANUAL, "Flagged by an admin"),
    ]

    hot_since = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text="When this product became worth players' attention. Recomputed "
        "daily, except where an admin has flagged it manually.",
    )
    hot_reason = models.CharField(max_length=20, choices=HOT_REASON_CHOICES, blank=True)
    hot_is_manual = models.BooleanField(
        default=False,
        help_text="Set by an admin for a recall or safety event. Survives the "
        "daily recompute, because the situations that need it are exactly the "
        "ones no metric will have noticed yet.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["canonical_name"]),
        ]

    @property
    def is_hot(self) -> bool:
        return self.hot_since is not None

    def generate_sqid(self) -> str:
        return Sqids(alphabet=settings.SQID_SALTS["product"]).encode([self.pk])

    def resolve_canonical(self) -> "Product":
        """Follow `merged_into` to the surviving record.

        Merges chain — A merges into B, B later merges into C — so resolution
        must be transitive or ratings stall on an intermediate record. Guards
        against cycles, which the admin merge action prevents but which a
        direct database edit could still introduce.
        """
        seen = {self.pk}
        node = self
        while node.merged_into_id is not None:
            if node.merged_into_id in seen:
                break
            seen.add(node.merged_into_id)
            node = node.merged_into
        return node

    def __str__(self) -> str:
        return self.canonical_name


class ProductUpc(models.Model):
    """A SKU-level barcode belonging to a product line.

    One-to-many by necessity: UPCs identify SKUs, so a product line with five
    sizes carries five of them.
    """

    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="upcs")
    upc = models.CharField(max_length=20, unique=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "product UPC"
        verbose_name_plural = "product UPCs"

    def __str__(self) -> str:
        return f"{self.upc} → {self.product.canonical_name}"


class ProductAlias(models.Model):
    """A receipt string known to mean a particular product.

    This is where convergence comes from. The key is the *raw receipt string*,
    normalised and scoped to the retailer that printed it — not the interpreted
    product name. Storing interpretations would store paraphrases of
    `canonical_name` against a record already called that, which adds almost no
    information and leaves every receipt re-running the same inference.

    Keying on raw strings makes matching an exact lookup, so each distinct
    (store, string) pair needs confirming once, by one player, ever — and every
    subsequent player who buys that product is never prompted.
    """

    STATUS_PROVISIONAL = "provisional"
    STATUS_AUTHORITATIVE = "authoritative"
    STATUS_DEMOTED = "demoted"
    STATUS_CHOICES = [
        (STATUS_PROVISIONAL, "Provisional"),
        (STATUS_AUTHORITATIVE, "Authoritative"),
        (STATUS_DEMOTED, "Demoted"),
    ]

    SOURCE_PLAYER = "player"
    SOURCE_ADJUDICATION = "adjudication"
    SOURCE_ADMIN = "admin"
    SOURCE_CHOICES = [
        (SOURCE_PLAYER, "Player confirmation"),
        (SOURCE_ADJUDICATION, "Model adjudication"),
        (SOURCE_ADMIN, "Admin"),
    ]

    product = models.ForeignKey(
        Product, on_delete=models.CASCADE, related_name="aliases"
    )
    store = models.ForeignKey(
        Store,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="product_aliases",
        help_text="The retailer that printed this string. Null means a global "
        "alias, matched only when no retailer-scoped alias applies.",
    )
    raw_text = models.CharField(max_length=300, help_text="Verbatim, as printed.")
    raw_text_normalised = models.CharField(max_length=300, db_index=True)
    source = models.CharField(
        max_length=20, choices=SOURCE_CHOICES, default=SOURCE_PLAYER
    )
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default=STATUS_PROVISIONAL
    )
    confirmation_count = models.PositiveIntegerField(default=0)
    contradiction_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "product aliases"
        constraints = [
            # Split in two because SQL treats NULLs as distinct: a single
            # unique_together over (store, raw_text_normalised) would allow
            # unlimited duplicate global aliases for the same string.
            models.UniqueConstraint(
                fields=["store", "raw_text_normalised"],
                condition=models.Q(store__isnull=False),
                name="unique_store_scoped_alias",
            ),
            models.UniqueConstraint(
                fields=["raw_text_normalised"],
                condition=models.Q(store__isnull=True),
                name="unique_global_alias",
            ),
        ]
        indexes = [
            models.Index(fields=["store", "raw_text_normalised"]),
            models.Index(fields=["status"]),
        ]

    needs_review = models.BooleanField(
        default=False,
        db_index=True,
        help_text="Set when players keep contradicting this alias. A string "
        "several people disagree about needs a human, not another vote.",
    )

    def save(self, *args: object, **kwargs: object) -> None:
        self.raw_text_normalised = normalise_raw_text(self.raw_text)
        super().save(*args, **kwargs)

    @property
    def net_confirmations(self) -> int:
        """Confirmations less contradictions. Status is derived from this."""
        return self.confirmation_count - self.contradiction_count

    def _recompute_status(self) -> None:
        required: int = settings.SPENDIUM["ALIAS_CONFIRMATIONS_REQUIRED"]
        net = self.net_confirmations
        if net >= required:
            self.status = self.STATUS_AUTHORITATIVE
        elif net > 0:
            self.status = self.STATUS_PROVISIONAL
        else:
            self.status = self.STATUS_DEMOTED

    def _recount(self) -> None:
        """Derive the counts from the votes rather than trusting a running total.

        Counting taps would let one player promote an alias to authoritative by
        confirming twice, which is the precise failure the two-confirmation rule
        exists to prevent. Deriving from one row per player makes that
        impossible rather than merely discouraged.
        """
        counts = self.votes.aggregate(
            yes=Count("pk", filter=Q(agreed=True)),
            no=Count("pk", filter=Q(agreed=False)),
        )
        self.confirmation_count = counts["yes"] or 0
        self.contradiction_count = counts["no"] or 0
        self._recompute_status()
        self.needs_review = (
            self.contradiction_count >= settings.SPENDIUM["ALIAS_REVIEW_CONTRADICTIONS"]
        )
        self.save()

    def record_vote(self, player: object, agreed: bool) -> None:
        """Store one player's verdict, replacing any earlier one from them.

        A player who changes their mind supersedes themselves rather than
        stacking, so nobody can outvote the crowd by clicking repeatedly.
        """
        AliasConfirmation.objects.update_or_create(
            alias=self, player=player, defaults={"agreed": agreed}
        )
        self._recount()

    def confirm(self, player: object) -> None:
        """Record agreement that this string means this product."""
        self.record_vote(player, agreed=True)

    def contradict(self, player: object) -> None:
        """Record disagreement, reopening the alias to prompting.

        Contradictions net against confirmations rather than jumping straight to
        demoted. An authoritative alias that one player disputes falls back to
        provisional and starts prompting again; it does not vanish on a single
        objection, and it does not survive a sustained one either.
        """
        self.record_vote(player, agreed=False)

    def __str__(self) -> str:
        scope = self.store.name if self.store else "global"
        return f"{self.raw_text} ({scope}) → {self.product.canonical_name}"


class ProductRatingSnapshot(models.Model):
    """A product's rating on one day.

    Ratings are computed over a rolling twelve-month window, so a past value
    cannot be reconstructed after the fact — the responses behind it age out.
    Recording them as they happen is the only way to show a trend later.
    """

    product = models.ForeignKey(
        Product, on_delete=models.CASCADE, related_name="rating_snapshots"
    )
    taken_on = models.DateField(db_index=True)
    score = models.DecimalField(max_digits=4, decimal_places=3)
    response_count = models.PositiveIntegerField(default=0)
    verified_count = models.PositiveIntegerField(default=0)
    # Recorded so a listing can tell whether a rating is publishable without
    # recomputing it. The k-anonymity gate is about purchases, not responses, so
    # without this a "best rated" page has to call `ratings.compute` per
    # candidate — which is what makes such a page too expensive to build.
    purchase_count = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["product", "taken_on"], name="one_snapshot_per_product_per_day"
            )
        ]
        ordering = ["taken_on"]

    def __str__(self) -> str:
        return f"{self.product.canonical_name} @ {self.taken_on}: {self.score}"


class StoreRatingSnapshot(models.Model):
    """A store's rating on one day.

    Same reasoning as `ProductRatingSnapshot`: the rating is computed over a
    rolling twelve-month window, so a past value cannot be reconstructed once
    the responses behind it age out.

    Carries `points_per_dollar` as well, which the product snapshot does not.
    For a store that is the number players actually choose on — "29 points per
    dollar versus 4" — so its history is worth at least as much as the
    percentage's, and it moves independently: a criterion crossing the k
    threshold changes the payout without moving the score at all.
    """

    store = models.ForeignKey(
        Store, on_delete=models.CASCADE, related_name="rating_snapshots"
    )
    taken_on = models.DateField(db_index=True)
    score = models.DecimalField(max_digits=4, decimal_places=3)
    response_count = models.PositiveIntegerField(default=0)
    verified_count = models.PositiveIntegerField(default=0)
    points_per_dollar = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["store", "taken_on"], name="one_snapshot_per_store_per_day"
            )
        ]
        ordering = ["taken_on"]

    def __str__(self) -> str:
        return f"{self.store.name} @ {self.taken_on}: {self.score}"


class EmergencyStop(models.Model):
    """The one control that stops Spendium spending money.

    Written for somebody's first day. They have been told the bill is running
    away, they are the only person available, and they should not need to know
    what a "tier" is or which model holds the setting. So: one checkbox, named
    for the situation rather than the mechanism, and it stops **everything** —
    extraction and adjudication alike.

    Stopping the narrower thing would be worse than useless. Adjudication only
    runs on the residual that exact and fuzzy matching miss, whereas extraction
    runs on every uploaded receipt, so pausing Tier 2 alone would look like
    pulling the switch and watching the meter keep spinning.

    Uploads are still accepted and still queued; receipts are simply not read
    until it is switched off, at which point a sweeper picks up everything that
    waited.

    That holds for a stop shorter than the image retention window. Receipt
    images are deleted 24 hours after upload no matter what — the commitment is
    published and an outage is the worst possible reason to quietly hold player
    photos longer — so a receipt uploaded into a stop that outlives it has no
    image left to read and fails. Once the stop has been on that long, new
    uploads are refused at the door rather than accepted and destroyed, and a
    failed receipt can always be uploaded again.
    """

    is_stopped = models.BooleanField(
        default=False,
        verbose_name="Stop all AI spending now",
        help_text=(
            "Tick this to stop Spendium calling any AI model. Receipts already "
            "uploaded stay safe and unread until you untick it, and are then "
            "processed automatically — nothing is lost and no player loses "
            "points. Untick to resume."
        ),
    )
    stopped_at = models.DateTimeField(null=True, blank=True)
    stopped_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    note = models.CharField(
        max_length=300,
        blank=True,
        help_text="Optional: what made you stop it. Useful to whoever asks later.",
    )

    class Meta:
        verbose_name = "Emergency stop"
        verbose_name_plural = "Emergency stop"

    def save(self, *args: object, **kwargs: object) -> None:
        # `stopped_at` is maintained here rather than in the admin because it is
        # no longer only for attribution: how long the stop has been on decides
        # whether uploads are still accepted. A stop pulled from a shell or a
        # test has to carry the same timestamp as one pulled from the admin.
        self.pk = 1
        if self.is_stopped:
            if self.stopped_at is None:
                self.stopped_at = timezone.now()
        else:
            self.stopped_at = None
            self.stopped_by = None
        super().save(*args, **kwargs)

    @classmethod
    def get(cls) -> "EmergencyStop":
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self) -> str:
        return "STOPPED — no AI spending" if self.is_stopped else "Running normally"


class MetricsSnapshot(models.Model):
    """One day's measurements, so convergence can be told from stagnation.

    The design's central claim is that the system improves without curation.
    Point-in-time numbers cannot support or refute that — a catalogue growing
    steadily while the prompt rate never falls looks identical to one that is
    working, unless you can see both trended.

    A null store is the whole platform. Per-store rows exist because the
    convergence that matters is per retailer: each chain's receipt strings are
    learned separately, so an overall average hides a chain that is not
    converging at all.
    """

    taken_on = models.DateField(db_index=True)
    store = models.ForeignKey(
        Store,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="metrics_snapshots",
        help_text="Null means the whole platform.",
    )

    line_items = models.PositiveIntegerField(default=0)
    alias_hits = models.PositiveIntegerField(default=0)
    fuzzy_matches = models.PositiveIntegerField(default=0)
    adjudicated = models.PositiveIntegerField(default=0)
    player_resolved = models.PositiveIntegerField(default=0)
    unmatched = models.PositiveIntegerField(default=0)

    prompts_pending = models.PositiveIntegerField(default=0)
    prompts_resolved = models.PositiveIntegerField(default=0)

    unverified_products = models.PositiveIntegerField(default=0)
    retired_products = models.PositiveIntegerField(default=0)
    demoted_aliases = models.PositiveIntegerField(default=0)
    aliases_needing_review = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["taken_on", "store"],
                name="one_metrics_snapshot_per_store_per_day",
            ),
            models.UniqueConstraint(
                fields=["taken_on"],
                condition=models.Q(store__isnull=True),
                name="one_platform_metrics_snapshot_per_day",
            ),
        ]
        ordering = ["taken_on"]

    @property
    def alias_hit_rate(self) -> float | None:
        """The headline convergence metric.

        Tier 0 hits are free, deterministic and need nobody's attention, so a
        rising share means the system is learning. A flat one means it is not,
        however much the catalogue has grown.
        """
        if not self.line_items:
            return None
        return self.alias_hits / self.line_items

    def __str__(self) -> str:
        where = self.store.name if self.store else "platform"
        return f"{where} @ {self.taken_on}"


class ActionCentreState(models.Model):
    """What a player has already seen, and what they have agreed to be sent.

    Separate from the Action Centre's contents, which are always derived. The
    badge is about *novelty*, and novelty is a fact about the player rather than
    about any item — which is why it cannot be stored on the items themselves.
    """

    player = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="action_centre_state",
    )
    last_visited_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Visiting clears the badge. It returns only when something "
        "genuinely new arrives, so it never becomes a permanent nag about items "
        "the player has already decided to ignore.",
    )
    emails_enabled = models.BooleanField(
        default=True,
        help_text="Opt-out is honoured immediately and permanently — including "
        "for the onboarding sequence.",
    )
    onboarding_emails_sent = models.PositiveIntegerField(default=0)
    last_email_at = models.DateTimeField(null=True, blank=True)

    @classmethod
    def get_for(cls, player: object) -> "ActionCentreState":
        state, _ = cls.objects.get_or_create(player=player)
        return state

    def __str__(self) -> str:
        return f"Action centre state for {self.player}"


class AliasConfirmation(models.Model):
    """One player's verdict on what a receipt string means.

    Exists so that "two independent confirmations" means two *people*. Without a
    row per player the counts are just a tally of taps, and a single mis-tapping
    player could promote a wrong alias to authoritative on their own — after
    which it would be applied silently to every future receipt carrying that
    string.
    """

    alias = models.ForeignKey(
        ProductAlias, on_delete=models.CASCADE, related_name="votes"
    )
    player = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="alias_votes"
    )
    agreed = models.BooleanField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["alias", "player"], name="one_alias_vote_per_player"
            )
        ]

    def __str__(self) -> str:
        verdict = "agreed" if self.agreed else "disagreed"
        return f"{self.player} {verdict}: {self.alias_id}"


class MatchConfig(models.Model):
    """Tunable matching thresholds, admin-editable.

    These are calibration, not design. None of them can be set meaningfully
    before real receipts exist, so they must be adjustable without a deploy —
    same reasoning as `SurveyConfig.min_survey_threshold`. The defaults are
    placeholders to be tuned against the labelled fixture set.
    """

    strong_match_score = models.PositiveIntegerField(
        default=90,
        help_text="At or above this score (0-100) a match is accepted silently, "
        "with no prompt.",
    )
    weak_match_score = models.PositiveIntegerField(
        default=72,
        help_text="At or above this, the best candidate is used but the player "
        "is offered a chance to correct it.",
    )
    noise_floor_score = models.PositiveIntegerField(
        default=55,
        help_text="Candidates below this are hidden entirely. Showing "
        "implausible options produces confusion, not signal.",
    )
    candidate_limit = models.PositiveIntegerField(
        default=200,
        help_text="How many candidates FTS5 narrowing hands to scoring. Caps "
        "matching cost so it does not grow with the catalogue.",
    )
    auto_merge_score = models.PositiveIntegerField(
        default=95,
        help_text="Two unverified records scoring at least this against each "
        "other are merged without asking. Deliberately higher than the strong "
        "match bar: a wrong merge is harder to notice than a missed one.",
    )
    adjudication_candidates = models.PositiveIntegerField(
        default=5,
        help_text="Candidates offered per item in the Tier 2 adjudication call. "
        "Set to 0 to disable adjudication entirely.",
    )
    # Strictly this is a ratings knob rather than a matching one, and the class
    # name is now slightly broader than it reads. It lives here because the
    # alternative was a settings constant, and a threshold meant to be ratcheted
    # up as players arrive must not need a deploy each time it moves — which is
    # this class's stated reason for existing. Worth renaming to SpendiumConfig
    # if a third non-matching knob turns up.
    min_rating_responses = models.PositiveIntegerField(
        default=0,
        help_text="Verified responses before a product rating is shown at all. "
        "0 means follow the shared survey threshold, which Polium also uses — "
        "set it here to let a catalogue being bootstrapped show something "
        "without changing what Polium pays out. Raise it as players arrive.",
    )
    retro_batch_size = models.PositiveIntegerField(
        default=500,
        help_text="Line items re-examined per retro-matching run, per layer. "
        "Retro-matching is never urgent, so a modest batch that always finishes "
        "beats a large one that times out halfway.",
    )
    prompt_budget = models.PositiveIntegerField(
        default=5,
        help_text="Maximum disambiguation prompts shown per receipt before the "
        "player asks for more. Players who see fifteen icons ignore all of "
        "them. Also caps work: one matching cascade runs per prompt shown, on "
        "every view of the purchase. Zero disables prompting entirely.",
    )

    class Meta:
        verbose_name = "Matching configuration"
        verbose_name_plural = "Matching configuration"

    def save(self, *args: object, **kwargs: object) -> None:
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def get(cls) -> "MatchConfig":
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self) -> str:
        return "Matching configuration"


class MatchTier(models.TextChoices):
    """Which tier of the matching cascade resolved a line item."""

    ALIAS = "alias", "Tier 0 — exact alias"
    FUZZY = "fuzzy", "Tier 1 — fuzzy match"
    ADJUDICATED = "adjudicated", "Tier 2 — model adjudication"
    PLAYER = "player", "Tier 3 — player confirmed"
    UNMATCHED = "unmatched", "Unmatched"


class LineItemFields(models.Model):
    """Fields shared by the player-linked and anonymous copies of a line item.

    Abstract, so both layers stay in step. `raw_text` in particular must survive
    anonymisation: once the receipt image is deleted it is the only durable
    record of what was bought, and retro-matching replays it as the catalogue
    grows.
    """

    raw_text = models.CharField(max_length=300, help_text="Verbatim, as printed.")
    raw_text_normalised = models.CharField(max_length=300, db_index=True)
    interpreted_name = models.CharField(
        max_length=300, blank=True, help_text="The model's reading of the raw text."
    )
    product = models.ForeignKey(
        Product,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="%(class)s_items",
    )
    match_tier = models.CharField(
        max_length=20, choices=MatchTier.choices, default=MatchTier.UNMATCHED
    )
    match_confidence = models.DecimalField(
        max_digits=4, decimal_places=3, null=True, blank=True
    )
    quantity = models.DecimalField(max_digits=8, decimal_places=3, default=1)
    unit_price = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )
    line_total = models.DecimalField(max_digits=10, decimal_places=2)
    retro_checked_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text="When retro-matching last examined this line. Ordering by it "
        "is what makes successive runs work through the backlog instead of "
        "re-examining the same head of the queue forever.",
    )

    class Meta:
        abstract = True

    def save(self, *args: object, **kwargs: object) -> None:
        self.raw_text_normalised = normalise_raw_text(self.raw_text)
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return self.raw_text


class Purchase(models.Model):
    """Layer 1 — the player-linked purchase record.

    Exists for `PURCHASE_RETENTION_DAYS` only. At expiry it is copied into the
    anonymous layer and **deleted**, which is what makes re-identification
    technically infeasible rather than merely prohibited by policy.

    The player FK is deliberately not nullable. Nulling it in place would leave
    the basket sitting in a table that other records — the points ledger among
    them — may reference, so the player could be recovered by joining back. A
    row that no longer exists cannot be joined to.
    """

    STATUS_PENDING = "pending"
    STATUS_PROCESSED = "processed"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Waiting to be read"),
        (STATUS_PROCESSED, "Read"),
        (STATUS_FAILED, "Could not be read"),
    ]

    player = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="purchases"
    )
    store = models.ForeignKey(
        Store,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="purchases",
    )
    METHOD_RECEIPT = "receipt"
    METHOD_QR = "qr"
    METHOD_SELF_REPORT = "self_report"
    METHOD_ONLINE = "online"
    METHOD_CHOICES = [
        (METHOD_RECEIPT, "Receipt photo"),
        (METHOD_QR, "QR code at the till"),
        (METHOD_SELF_REPORT, "Self-reported"),
        (METHOD_ONLINE, "Online order"),
    ]

    processing_status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_PENDING,
        db_index=True,
        help_text="Extraction runs in a task, so a purchase exists before it "
        "has been read.",
    )
    verification_method = models.CharField(
        max_length=20,
        choices=METHOD_CHOICES,
        default=METHOD_RECEIPT,
        help_text="How the purchase was evidenced. Only the receipt path exists "
        "so far; the others earn at a reduced rate when they are built, because "
        "an unevidenced claim is worth less than a photographed till roll.",
    )
    points_awarded = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Set once, when the receipt is first read. Its presence is "
        "what stops a reprocessed or re-matched purchase paying out twice.",
    )

    HOLD_VELOCITY = "velocity"
    HOLD_HIGH_VALUE = "high_value"
    HOLD_CHOICES = [
        (HOLD_VELOCITY, "Unusually many receipts in a short time"),
        (HOLD_HIGH_VALUE, "Unusually large receipt"),
    ]
    hold_reason = models.CharField(
        max_length=20,
        choices=HOLD_CHOICES,
        blank=True,
        db_index=True,
        help_text="Withholds points pending review. The receipt is still read "
        "and still counts toward ratings — holding the data as well as the "
        "reward would punish the honest majority to inconvenience a few.",
    )
    held_at = models.DateTimeField(null=True, blank=True)
    processing_problems = models.JSONField(
        default=list,
        blank=True,
        help_text="Arithmetic and legibility problems found during extraction. "
        "Shown to the player rather than hidden — they are the only person who "
        "can tell whether a reading is actually wrong.",
    )
    purchased_at = models.DateTimeField(
        help_text="Transaction time, from the receipt. Holds the upload time "
        "until the receipt has been read."
    )
    subtotal = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )
    tax = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    total = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0"))
    receipt_image = models.FileField(
        upload_to="receipts/%Y/%m/",
        null=True,
        blank=True,
        help_text="Transient. Deleted within IMAGE_RETENTION_HOURS of "
        "processing, per the published privacy policy.",
    )
    image_phash = models.CharField(
        max_length=64,
        blank=True,
        db_index=True,
        help_text="Perceptual hash for duplicate detection. Outlives the image "
        "itself, which is deleted within IMAGE_RETENTION_HOURS.",
    )
    image_deleted_at = models.DateTimeField(null=True, blank=True)
    anonymise_after = models.DateTimeField(
        db_index=True,
        help_text="When this record must be anonymised. A sweeper catches any "
        "purchase whose scheduled task was lost.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["player", "purchased_at"])]

    def save(self, *args: object, **kwargs: object) -> None:
        if not self.anonymise_after:
            days: int = settings.SPENDIUM["PURCHASE_RETENTION_DAYS"]
            self.anonymise_after = timezone.now() + timedelta(days=days)
        super().save(*args, **kwargs)

    @property
    def window_is_open(self) -> bool:
        """Whether the player may still rate and disambiguate this purchase."""
        return timezone.now() < self.anonymise_after

    def __str__(self) -> str:
        store = self.store.name if self.store else "unknown store"
        return f"{store} — {self.purchased_at:%Y-%m-%d} ({self.total})"


class PurchaseLineItem(LineItemFields):
    STATE_NOT_NEEDED = "not_needed"
    STATE_PENDING = "pending"
    STATE_RESOLVED = "resolved"
    STATE_CHOICES = [
        (STATE_NOT_NEEDED, "No disambiguation needed"),
        (STATE_PENDING, "Awaiting player confirmation"),
        (STATE_RESOLVED, "Resolved by player"),
    ]

    purchase = models.ForeignKey(
        Purchase, on_delete=models.CASCADE, related_name="line_items"
    )
    disambiguation_state = models.CharField(
        max_length=20, choices=STATE_CHOICES, default=STATE_NOT_NEEDED
    )

    class Meta:
        indexes = [models.Index(fields=["disambiguation_state"])]


class AnonymisedPurchase(models.Model):
    """Layer 2 — permanent, anonymous, no path back to a player.

    Written at anonymisation time rather than at processing time. A row created
    up front could never absorb the corrections a player makes during their
    window, so the analytical layer would permanently record product identities
    the player had already fixed.

    `purchase_token` is freshly generated here and has no relationship to the
    player or to the original row's primary key. Its only job is to hold a
    basket together for co-purchase analysis.
    """

    purchase_token = models.UUIDField(default=uuid4, unique=True, editable=False)
    store = models.ForeignKey(
        Store,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="anonymised_purchases",
    )
    purchased_at = models.DateTimeField()
    total = models.DecimalField(max_digits=10, decimal_places=2)
    anonymised_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["purchased_at"])]

    def __str__(self) -> str:
        return f"{self.purchase_token} ({self.purchased_at:%Y-%m-%d})"


class AnonymisedLineItem(LineItemFields):
    anonymised_purchase = models.ForeignKey(
        AnonymisedPurchase, on_delete=models.CASCADE, related_name="line_items"
    )
