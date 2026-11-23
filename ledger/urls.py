from django.urls import path

from .api import catalog as catalog_api
from .api import collection as collection_api
from .api import extension as extension_api
from .api import games as games_api
from .api import orders as orders_api
from .api import locations as locations_api
from .api import purchases as purchases_api
from .api import reports as reports_api
from .api import repricing as repricing_api
from .api import saved_views as saved_views_api
from .api import overview as overview_api
from .api import sale as sale_api
from .api import shipments as shipments_api
from .api import scan as scan_api
from .api import sync as sync_api

app_name = "ledger"

urlpatterns = [
    path("api/choices/", collection_api.choices, name="api_choices"),
    path("api/games/", games_api.games, name="api_games"),
    path("api/collection/", collection_api.collection, name="api_collection"),
    path("api/collection/<int:pk>/", collection_api.item, name="api_item"),
    path("api/collection/bulk/", collection_api.bulk, name="api_collection_bulk"),
    path("api/collection/import/", collection_api.import_csv, name="api_import"),
    path("api/collection/export/", collection_api.export_csv, name="api_export"),
    path("api/collection/views/", saved_views_api.views, name="api_collection_views"),
    path("api/collection/views/<int:pk>/", saved_views_api.view, name="api_collection_view"),
    path("api/listings/", sale_api.listings, name="api_listings"),
    path("api/listings/bulk/", sale_api.bulk, name="api_listings_bulk"),
    path("api/listings/<int:item_pk>/", sale_api.listing, name="api_listing"),
    path("api/catalog/", catalog_api.catalog, name="api_catalog"),
    path("api/catalog/categories/", catalog_api.categories, name="api_catalog_categories"),
    path("api/catalog/report-missing/", catalog_api.report_missing, name="api_catalog_report_missing"),
    path("api/catalog/<int:ct_id>/history/", catalog_api.history, name="api_catalog_history"),
    path("api/purchases/", purchases_api.lots, name="api_purchases"),
    path("api/purchases/next-code/", purchases_api.next_code, name="api_purchases_next_code"),
    path("api/purchases/<int:pk>/", purchases_api.lot, name="api_purchase"),
    path("api/purchases/<int:pk>/attachments/", purchases_api.attachments, name="api_purchase_attachments"),
    path("api/purchases/<int:pk>/attachments/<int:attachment_pk>/", purchases_api.attachment,
         name="api_purchase_attachment"),
    path("api/purchases/<int:pk>/list/", purchases_api.list_lot, name="api_purchase_list"),
    path("api/purchases/<int:pk>/allocate-cost/", purchases_api.allocate_cost, name="api_purchase_allocate_cost"),
    path("api/reports/", reports_api.reports, name="api_reports"),
    path("api/reports/sets.csv", reports_api.sets_csv, name="api_reports_sets_csv"),
    path("api/locations/", locations_api.locations, name="api_locations"),
    path("api/locations/suggest/", locations_api.suggest, name="api_locations_suggest"),
    path("api/locations/<int:pk>/", locations_api.location, name="api_location"),
    path("api/overview/", overview_api.overview, name="api_overview"),
    path("api/orders/", orders_api.orders, name="api_orders"),
    path("api/orders/pick-list/", orders_api.pick_list, name="api_orders_pick_list"),
    path("api/orders/slips/", orders_api.slips, name="api_orders_slips"),
    path("api/orders/<int:pk>/", orders_api.order, name="api_order"),
    path("api/orders/<int:pk>/items/<int:item_pk>/", orders_api.order_item, name="api_order_item"),
    path("api/orders/<int:pk>/pack/", orders_api.pack, name="api_order_pack"),
    path("api/orders/<int:pk>/unpack/", orders_api.unpack, name="api_order_unpack"),
    path("api/orders/<int:pk>/issue/", orders_api.issue, name="api_order_issue"),
    path("api/shipments/", shipments_api.shipments, name="api_shipments"),
    path("api/shipments/<int:pk>/", shipments_api.shipment, name="api_shipment"),
    path("api/repricing/rules/", repricing_api.rules, name="api_repricing_rules"),
    path("api/repricing/rules/reorder/", repricing_api.reorder, name="api_repricing_reorder"),
    path("api/repricing/rules/preview/", repricing_api.rule_preview, name="api_repricing_rule_preview"),
    path("api/repricing/rules/<int:pk>/", repricing_api.rule, name="api_repricing_rule"),
    path("api/repricing/preview/", repricing_api.preview, name="api_repricing_preview"),
    path("api/repricing/run/", repricing_api.run, name="api_repricing_run"),
    path("api/repricing/runs/", repricing_api.runs, name="api_repricing_runs"),
    path("api/repricing/runs/<int:pk>/", repricing_api.run_detail, name="api_repricing_run_detail"),
    path("api/repricing/quote/", repricing_api.quote, name="api_repricing_quote"),
    path("api/repricing/adhoc/", repricing_api.adhoc, name="api_repricing_adhoc"),
    path("api/sync/", sync_api.sync, name="api_sync"),
    path("api/sync/conflicts/<int:pk>/resolve/", sync_api.resolve, name="api_resolve"),
    path("api/extension/pair/", extension_api.pair, name="api_extension_pair"),
    path("api/extension/claim/", extension_api.claim, name="api_extension_claim"),
    path("api/extension/tokens/", extension_api.tokens, name="api_extension_tokens"),
    path("api/extension/tokens/<int:pk>/", extension_api.token, name="api_extension_token"),
    path("api/extension/status/", extension_api.status, name="api_extension_status"),
    path("api/extension/plan/", extension_api.plan, name="api_extension_plan"),
    path("api/extension/report/", extension_api.report, name="api_extension_report"),
    path("api/extension/shipped/", extension_api.shipped, name="api_extension_shipped"),
    path("api/scan/setup/", scan_api.setup, name="scan_api_setup"),
    path("api/scan/sessions/", scan_api.sessions, name="scan_api_sessions"),
    path("api/scan/sessions/<uuid:uid>/", scan_api.session_detail, name="scan_api_session"),
    path("api/scan/sessions/<uuid:uid>/captures/", scan_api.captures, name="scan_api_captures"),
    path("api/scan/sessions/<uuid:uid>/search/", scan_api.search, name="scan_api_search"),
    path("api/scan/sessions/<uuid:uid>/manual/", scan_api.manual, name="scan_api_manual"),
    path("api/scan/sessions/<uuid:uid>/review/", scan_api.review, name="scan_api_review"),
    path("api/scan/sessions/<uuid:uid>/save/", scan_api.save, name="scan_api_save"),
    path("api/scan/lines/<uuid:uid>/", scan_api.line_detail, name="scan_api_line"),
    path("api/scan/lines/<uuid:uid>/undo/", scan_api.line_undo, name="scan_api_undo"),
    path("api/scan/lines/<uuid:uid>/confirm/", scan_api.line_confirm, name="scan_api_confirm"),
    path("api/scan/lines/<uuid:uid>/frame.jpg", scan_api.line_frame, name="scan_api_frame"),
]
