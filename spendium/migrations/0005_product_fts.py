from django.db import migrations

from spendium.search import TABLE

# Standalone rather than an FTS5 external-content table: the searchable text is
# derived (a product's canonical name plus every alias resolving to it), not a
# column-for-column mirror of one source table. `spendium.search` keeps it in
# step via signals, and `rebuild_product_index` repopulates it in bulk.
#
# product_id is UNINDEXED so it is stored and returned but contributes nothing
# to relevance — matching a product's primary key would be meaningless noise.
CREATE = f"""
CREATE VIRTUAL TABLE IF NOT EXISTS {TABLE} USING fts5(
    search_text,
    product_id UNINDEXED,
    tokenize='unicode61'
);
"""

DROP = f"DROP TABLE IF EXISTS {TABLE};"


class Migration(migrations.Migration):
    dependencies = [
        ("spendium", "0004_matchconfig"),
    ]

    operations = [
        migrations.RunSQL(sql=CREATE, reverse_sql=DROP),
    ]
