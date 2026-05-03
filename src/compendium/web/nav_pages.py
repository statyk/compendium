"""Curated list of internal app pages available as nav shortcut destinations."""
from __future__ import annotations

NAV_PAGES: list[dict] = [
    # ── Catalog ───────────────────────────────────────────────────────────────
    {"key": "catalog", "label": "Catalog Search", "url": "/ui/catalog", "permission": None},
    # ── Circulation ───────────────────────────────────────────────────────────
    {"key": "circ_desk", "label": "Circ Desk", "url": "/ui/circ", "permission": "loan.checkout"},
    {"key": "kiosk", "label": "Kiosk", "url": "/ui/kiosk", "permission": "loan.checkout"},
    {"key": "patrons", "label": "Patrons", "url": "/ui/patrons", "permission": "patron.manage"},
    {"key": "active_loans", "label": "Active Loans", "url": "/ui/admin/loans", "permission": "loan.view.any"},
    {"key": "outstanding_fines", "label": "Outstanding Fines", "url": "/ui/admin/fines", "permission": "fine.manage"},
    {"key": "holds", "label": "Holds Queue", "url": "/ui/admin/holds", "permission": "hold.view.any"},
    {"key": "claims", "label": "Claims Returned", "url": "/ui/admin/claims", "permission": "loan.checkin"},
    # ── Cataloging ────────────────────────────────────────────────────────────
    {"key": "new_item", "label": "Add Item", "url": "/ui/items/new", "permission": "item.create"},
    {"key": "import_catalog", "label": "Import", "url": "/ui/admin/import", "permission": "catalog.import"},
    {"key": "export_catalog", "label": "Export", "url": "/ui/admin/export", "permission": "item.view"},
    {"key": "labels", "label": "Labels", "url": "/ui/labels", "permission": "labels.generate"},
    # ── Admin ─────────────────────────────────────────────────────────────────
    {"key": "patron_categories", "label": "Patron Categories", "url": "/ui/admin/patron-categories", "permission": "patron.manage"},
    {"key": "policies", "label": "Loan Policies", "url": "/ui/policies", "permission": "policy.edit"},
    {"key": "branches", "label": "Branches", "url": "/ui/branches", "permission": "branch.edit"},
    {"key": "notifications", "label": "Notifications", "url": "/ui/admin/notifications", "permission": "notification.manage"},
    {"key": "reports", "label": "Reports", "url": "/ui/reports", "permission": "report.view"},
    {"key": "settings_general", "label": "General Settings", "url": "/ui/admin/settings/general", "permission": "patron.manage"},
    {"key": "settings_circulation", "label": "Circulation Settings", "url": "/ui/admin/settings/circulation", "permission": "patron.manage"},
    {"key": "settings_kiosk", "label": "Kiosk Settings", "url": "/ui/admin/settings/kiosk", "permission": "patron.manage"},
    {"key": "audit_log", "label": "Audit Log", "url": "/ui/audit", "permission": "audit.view"},
    # ── System ────────────────────────────────────────────────────────────────
    {"key": "users", "label": "Users", "url": "/ui/users", "permission": "user.manage"},
    {"key": "roles", "label": "Roles", "url": "/ui/roles", "permission": "role.manage"},
    {"key": "settings_smtp", "label": "SMTP Settings", "url": "/ui/admin/system/smtp", "permission": "system.manage"},
    {"key": "settings_retention", "label": "Retention Settings", "url": "/ui/admin/system/retention", "permission": "system.manage"},
    {"key": "settings_security", "label": "Security Settings", "url": "/ui/admin/system/security", "permission": "system.manage"},
    # ── Self-service ─────────────────────────────────────────────────────────
    {"key": "my_loans", "label": "My Loans", "url": "/ui/me/loans", "permission": None},
    {"key": "my_holds", "label": "My Holds", "url": "/ui/me/holds", "permission": None},
    {"key": "my_fines", "label": "My Fines", "url": "/ui/me/fines", "permission": "fine.view.self"},
]
