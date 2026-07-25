from django.conf import settings
from django.db import models
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

    def confirm(self) -> None:
        """Record agreement that this string means this product.

        Independence — that each confirmation comes from a different player — is
        not enforced here. That check needs a per-confirmation record to compare
        against, which arrives with the disambiguation flow.
        """
        self.confirmation_count += 1
        self._recompute_status()
        self.save()

    def contradict(self) -> None:
        """Record disagreement, reopening the alias to prompting.

        Contradictions net against confirmations rather than jumping straight to
        demoted. An authoritative alias that one player disputes falls back to
        provisional and starts prompting again; it does not vanish on a single
        objection, and it does not survive a sustained one either.
        """
        self.contradiction_count += 1
        self._recompute_status()
        self.save()

    def __str__(self) -> str:
        scope = self.store.name if self.store else "global"
        return f"{self.raw_text} ({scope}) → {self.product.canonical_name}"
