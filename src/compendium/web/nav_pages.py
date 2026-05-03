"""Curated list of internal app pages available as nav shortcut destinations."""
from __future__ import annotations

NAV_PAGES: list[dict] = [
    # ── Catalog ───────────────────────────────────────────────────────────────
    {"key": "catalog", "section": "Catalog", "label": "Catalog Search", "url": "/ui/catalog", "permission": None},
    # ── Circulation ───────────────────────────────────────────────────────────
    {"key": "circ_desk", "section": "Circulation", "label": "Circ Desk", "url": "/ui/circ", "permission": "loan.checkout"},
    {"key": "kiosk", "section": "Circulation", "label": "Kiosk", "url": "/ui/kiosk", "permission": "loan.checkout"},
    {"key": "patrons", "section": "Circulation", "label": "Patrons", "url": "/ui/patrons", "permission": "patron.manage"},
    {"key": "active_loans", "section": "Circulation", "label": "Active Loans", "url": "/ui/admin/loans", "permission": "loan.view.any"},
    {"key": "outstanding_fines", "section": "Circulation", "label": "Outstanding Fines", "url": "/ui/admin/fines", "permission": "fine.manage"},
    {"key": "holds", "section": "Circulation", "label": "Holds Queue", "url": "/ui/admin/holds", "permission": "hold.view.any"},
    {"key": "claims", "section": "Circulation", "label": "Claims Returned", "url": "/ui/admin/claims", "permission": "loan.checkin"},
    # ── Cataloging ────────────────────────────────────────────────────────────
    {"key": "new_item", "section": "Cataloging", "label": "Add Item", "url": "/ui/items/new", "permission": "item.create"},
    {"key": "import_catalog", "section": "Cataloging", "label": "Import", "url": "/ui/admin/import", "permission": "catalog.import"},
    {"key": "export_catalog", "section": "Cataloging", "label": "Export", "url": "/ui/admin/export", "permission": "item.view"},
    {"key": "labels", "section": "Cataloging", "label": "Labels", "url": "/ui/labels", "permission": "labels.generate"},
    # ── Admin ─────────────────────────────────────────────────────────────────
    {"key": "patron_categories", "section": "Admin", "label": "Patron Categories", "url": "/ui/admin/patron-categories", "permission": "patron.manage"},
    {"key": "policies", "section": "Admin", "label": "Loan Policies", "url": "/ui/policies", "permission": "policy.edit"},
    {"key": "branches", "section": "Admin", "label": "Branches", "url": "/ui/branches", "permission": "branch.edit"},
    {"key": "notifications", "section": "Admin", "label": "Notifications", "url": "/ui/admin/notifications", "permission": "notification.manage"},
    {"key": "reports", "section": "Admin", "label": "Reports", "url": "/ui/reports", "permission": "report.view"},
    {"key": "settings_general", "section": "Admin", "label": "General Settings", "url": "/ui/admin/settings/general", "permission": "patron.manage"},
    {"key": "settings_circulation", "section": "Admin", "label": "Circulation Settings", "url": "/ui/admin/settings/circulation", "permission": "patron.manage"},
    {"key": "settings_kiosk", "section": "Admin", "label": "Kiosk Settings", "url": "/ui/admin/settings/kiosk", "permission": "patron.manage"},
    {"key": "audit_log", "section": "Admin", "label": "Audit Log", "url": "/ui/audit", "permission": "audit.view"},
    # ── System ────────────────────────────────────────────────────────────────
    {"key": "users", "section": "System", "label": "Users", "url": "/ui/users", "permission": "user.manage"},
    {"key": "roles", "section": "System", "label": "Roles", "url": "/ui/roles", "permission": "role.manage"},
    {"key": "settings_smtp", "section": "System", "label": "SMTP Settings", "url": "/ui/admin/system/smtp", "permission": "system.manage"},
    {"key": "settings_retention", "section": "System", "label": "Retention Settings", "url": "/ui/admin/system/retention", "permission": "system.manage"},
    {"key": "settings_security", "section": "System", "label": "Security Settings", "url": "/ui/admin/system/security", "permission": "system.manage"},
    # ── Self-service ─────────────────────────────────────────────────────────
    {"key": "my_loans", "section": "Self-service", "label": "My Loans", "url": "/ui/me/loans", "permission": None},
    {"key": "my_holds", "section": "Self-service", "label": "My Holds", "url": "/ui/me/holds", "permission": None},
    {"key": "my_fines", "section": "Self-service", "label": "My Fines", "url": "/ui/me/fines", "permission": "fine.view.self"},
]
