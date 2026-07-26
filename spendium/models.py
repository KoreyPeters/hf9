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
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["canonical_name"]),
        ]

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
    prompt_budget = models.PositiveIntegerField(
        default=5,
        help_text="Maximum disambiguation prompts shown per receipt. Players "
        "who see fifteen icons ignore all of them.",
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
    processing_status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_PENDING,
        db_index=True,
        help_text="Extraction runs in a task, so a purchase exists before it "
        "has been read.",
    )
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
