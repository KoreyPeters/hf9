import accounts.task_views as accounts_task_views
import lifecycle.task_views as lifecycle_task_views
import polium.task_views as polium_task_views
import spendium.task_views as spendium_task_views
from django.urls import path

urlpatterns = [
    path(
        "check-deprecations/",
        lifecycle_task_views.check_deprecations,
        name="task_check_deprecations",
    ),
    path(
        "check-deletions/",
        lifecycle_task_views.check_deletions,
        name="task_check_deletions",
    ),
    path(
        "update-candidate-rating/",
        polium_task_views.update_candidate_rating,
        name="task_update_candidate_rating",
    ),
    path(
        "verify-email-reminder/",
        accounts_task_views.send_verification_reminder,
        name="task_verify_email_reminder",
    ),
    path(
        "anonymise-purchase/",
        spendium_task_views.anonymise_purchase,
        name="task_anonymise_purchase",
    ),
    path(
        "sweep-purchase-anonymisation/",
        spendium_task_views.sweep_purchase_anonymisation,
        name="task_sweep_purchase_anonymisation",
    ),
    path(
        "process-receipt/",
        spendium_task_views.process_receipt,
        name="task_process_receipt",
    ),
    path("retro-match/", spendium_task_views.retro_match, name="task_retro_match"),
    path(
        "recompute-hotness/",
        spendium_task_views.recompute_hotness,
        name="task_recompute_hotness",
    ),
    path(
        "sweep-pending-receipts/",
        spendium_task_views.sweep_pending_receipts,
        name="task_sweep_pending_receipts",
    ),
    path(
        "snapshot-metrics/",
        spendium_task_views.snapshot_metrics,
        name="task_snapshot_metrics",
    ),
    path(
        "send-action-centre-emails/",
        spendium_task_views.send_action_centre_emails,
        name="task_send_action_centre_emails",
    ),
    path(
        "snapshot-ratings/",
        spendium_task_views.snapshot_ratings,
        name="task_snapshot_ratings",
    ),
    path(
        "delete-receipt-image/",
        spendium_task_views.delete_receipt_image,
        name="task_delete_receipt_image",
    ),
    path(
        "sweep-receipt-images/",
        spendium_task_views.sweep_receipt_images,
        name="task_sweep_receipt_images",
    ),
]
