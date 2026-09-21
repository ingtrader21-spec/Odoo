{
    "name": "Codestra Campaign Control Plane",
    "version": "19.0.1.0.0",
    "summary": "Canonical campaign control lifecycle, configuration versions, templates, and workspace projection",
    "author": "Codestra",
    "license": "LGPL-3",
    "depends": [
        "codestra_cc_core",
        "codestra_cc_crm",
        "codestra_cc_security",
        "codestra_campaign_crm_os",
        "codestra_identity_provisioning",
        "call_center_campaign",
    ],
    "data": [
        "security/ir.model.access.csv",
        "security/record_rules.xml",
        "views/control_plane_views.xml",
        "views/menus.xml",
    ],
    "installable": True,
    "application": False,
}
