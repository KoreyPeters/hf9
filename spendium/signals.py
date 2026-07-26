"""Keep the FTS5 narrowing index in step with the catalogue.

Signals rather than explicit calls because the catalogue is written from several
directions — the seed importer, the matching pipeline, player disambiguation and
the admin merge action. An index that silently drifts produces missed matches
that look like ordinary fuzzy-matching failures, so it is worth catching every
write path rather than remembering to call the indexer at each one.
"""

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from . import search
from .models import Product, ProductAlias


@receiver(post_save, sender=Product)
def reindex_on_product_save(sender: type, instance: Product, **kwargs: object) -> None:
    search.index_product(instance.pk)


@receiver(post_delete, sender=Product)
def deindex_on_product_delete(
    sender: type, instance: Product, **kwargs: object
) -> None:
    search.remove_product(instance.pk)


@receiver(post_save, sender=ProductAlias)
def reindex_on_alias_save(
    sender: type, instance: ProductAlias, **kwargs: object
) -> None:
    search.index_product(instance.product_id)


@receiver(post_delete, sender=ProductAlias)
def reindex_on_alias_delete(
    sender: type, instance: ProductAlias, **kwargs: object
) -> None:
    # The product survives the alias, so this is a refresh, not a removal.
    search.index_product(instance.product_id)
