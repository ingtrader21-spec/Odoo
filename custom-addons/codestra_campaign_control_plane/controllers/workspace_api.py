from odoo import http
from odoo.http import request


class CampaignWorkspaceApi(http.Controller):
    """Calculated workspace projection; no workspace business model is created."""

    @http.route(
        "/cc/api/v1/me/workspace",
        type="jsonrpc",
        auth="user",
        methods=["POST"],
        csrf=False,
    )
    def workspace(self, campaign_id=None, limit=50, **kwargs):
        user = request.env.user
        domain = [
            ("user_id", "=", user.id),
            ("state", "=", "active"),
        ]
        if campaign_id:
            domain.append(("campaign_id", "=", int(campaign_id)))
        memberships = request.env["cc.campaign.membership"].search(
            domain, order="campaign_id, role", limit=min(max(int(limit), 1), 200)
        )
        campaigns = memberships.mapped("campaign_id")
        profiles = request.env["cc.customer.profile"].search(
            [
                ("assigned_user_id", "=", user.id),
                ("campaign_id", "in", campaigns.ids),
                ("active", "=", True),
            ],
            order="write_date desc",
            limit=50,
        )
        channels = request.env["codestra.agent.channel"].search(
            [("odoo_user_id", "=", user.id), ("campaign_id", "in", campaigns.ids)]
        )
        items = request.env["cc.workspace.item"].search(
            [
                ("campaign_id", "in", campaigns.ids),
                ("visible_to_agent", "=", True),
                ("active", "=", True),
            ],
            order="campaign_id, section_id, sequence, id",
        )
        return {
            "user_id": user.id,
            "campaigns": [
                {
                    "id": campaign.id,
                    "code": campaign.code,
                    "name": campaign.name,
                    "lifecycle_state": campaign.lifecycle_state,
                    "effective_state": campaign.effective_state,
                    "application": campaign.application_id.code
                    if campaign.application_id
                    else None,
                    "template_version": campaign.template_version_id.version
                    if campaign.template_version_id
                    else None,
                }
                for campaign in campaigns
            ],
            "memberships": [
                {
                    "id": membership.id,
                    "campaign_id": membership.campaign_id.id,
                    "role": membership.role,
                    "state": membership.state,
                }
                for membership in memberships
            ],
            "profiles": [
                {
                    "id": profile.id,
                    "profile_uuid": profile.profile_uuid,
                    "campaign_id": profile.campaign_id.id,
                    "name": profile.name,
                    "state": profile.state,
                }
                for profile in profiles
            ],
            "channels": [
                {
                    "id": channel.id,
                    "campaign_id": channel.campaign_id.id,
                    "channel_type": channel.channel_type,
                    "desired_enabled": channel.desired_enabled,
                    "state": channel.state,
                    "effective_access": channel.effective_access,
                }
                for channel in channels
            ],
            "layout": [
                {
                    "id": item.id,
                    "campaign_id": item.campaign_id.id,
                    "section_id": item.section_id.id,
                    "name": item.name,
                    "item_type": item.item_type,
                    "reference_key": item.reference_key,
                    "sequence": item.sequence,
                    "required": item.required,
                }
                for item in items
            ],
        }

    @http.route(
        "/cc/api/v1/applications",
        type="jsonrpc",
        auth="user",
        methods=["POST"],
        csrf=False,
    )
    def applications(self, active_only=True, **kwargs):
        domain = [("active", "=", True)] if active_only else []
        records = request.env["cc.application"].search(domain, order="sequence, code")
        return [
            {"id": record.id, "code": record.code, "name": record.name}
            for record in records
        ]

    @http.route(
        "/cc/api/v1/templates",
        type="jsonrpc",
        auth="user",
        methods=["POST"],
        csrf=False,
    )
    def templates(self, application_id=None, **kwargs):
        domain = [("active", "=", True)]
        if application_id:
            domain.append(("application_id", "=", int(application_id)))
        records = request.env["cc.campaign.template"].search(domain, order="code")
        return [
            {
                "id": record.id,
                "code": record.code,
                "name": record.name,
                "application_id": record.application_id.id,
                "current_version": record.current_version_id.version
                if record.current_version_id
                else None,
            }
            for record in records
        ]

    @http.route(
        "/cc/api/v1/templates/<int:template_id>",
        type="jsonrpc",
        auth="user",
        methods=["POST"],
        csrf=False,
    )
    def template_detail(self, template_id, **kwargs):
        template = request.env["cc.campaign.template"].browse(template_id).exists()
        if not template:
            return {"error": "template_not_found"}
        versions = request.env["cc.campaign.template.version"].search(
            [("template_id", "=", template.id)], order="version desc"
        )
        return {
            "id": template.id,
            "code": template.code,
            "name": template.name,
            "application_id": template.application_id.id,
            "versions": [
                {
                    "id": version.id,
                    "version": version.version,
                    "state": version.state,
                    "content_hash": version.content_hash,
                    "fields": [
                        {
                            "id": field.id,
                            "key": field.key,
                            "label": field.label,
                            "field_type": field.field_type,
                            "required": field.required,
                            "masked": field.masked,
                            "visible_to_agent": field.visible_to_agent,
                            "reportable": field.reportable,
                            "validation": field.validation_json,
                            "options": field.options_json,
                        }
                        for field in version.field_definition_ids
                    ],
                }
                for version in versions
            ],
        }
