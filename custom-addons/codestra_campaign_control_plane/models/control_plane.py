import hashlib
import json

from odoo import api, fields, models
from odoo.exceptions import AccessError, ValidationError

CONFIGURATION_CAPABILITY = object()


def _canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class CcApplication(models.Model):
    _name = "cc.application"
    _description = "Contact Center Application Registry"
    _order = "sequence, code"

    name = fields.Char(required=True)
    code = fields.Char(required=True, index=True)
    description = fields.Text()
    active = fields.Boolean(default=True, required=True)
    sequence = fields.Integer(default=10)
    _code_unique = models.Constraint("unique(code)", "Application codes must be unique.")


class CcCampaignTemplate(models.Model):
    _name = "cc.campaign.template"
    _description = "Campaign Template"
    _order = "code"

    name = fields.Char(required=True)
    code = fields.Char(required=True, index=True)
    application_id = fields.Many2one("cc.application", required=True, ondelete="restrict")
    description = fields.Text()
    active = fields.Boolean(default=True, required=True)
    current_version_id = fields.Many2one(
        "cc.campaign.template.version", readonly=True, copy=False
    )
    version_ids = fields.One2many(
        "cc.campaign.template.version", "template_id", readonly=True
    )
    _code_unique = models.Constraint("unique(code)", "Template codes must be unique.")


class CcCampaignTemplateVersion(models.Model):
    _name = "cc.campaign.template.version"
    _description = "Immutable Campaign Template Version"
    _order = "template_id, version desc"

    template_id = fields.Many2one("cc.campaign.template", required=True, ondelete="restrict")
    version = fields.Integer(required=True, readonly=True)
    state = fields.Selection(
        [("draft", "Draft"), ("approved", "Approved"), ("retired", "Retired")],
        required=True,
        default="draft",
        readonly=True,
    )
    layout_json = fields.Json(required=True, readonly=True, default=dict)
    workflow_json = fields.Json(required=True, readonly=True, default=dict)
    field_definition_ids = fields.One2many(
        "cc.field.definition", "template_version_id", readonly=True
    )
    content_hash = fields.Char(required=True, size=64, readonly=True)
    approved_by = fields.Many2one("res.users", readonly=True)
    approved_at = fields.Datetime(readonly=True)
    _version_unique = models.Constraint(
        "unique(template_id, version)", "Template versions must be unique."
    )

    @api.model_create_multi
    def create(self, values_list):
        if not self.env.user.has_group("codestra_cc_security.group_cc_global_administrator"):
            raise AccessError("Template versions require global administrator access.")
        prepared = []
        for values in values_list:
            values = dict(values)
            payload = {
                "layout": values.get("layout_json") or {},
                "workflow": values.get("workflow_json") or {},
            }
            values["content_hash"] = hashlib.sha256(
                _canonical_json(payload).encode()
            ).hexdigest()
            prepared.append(values)
        return super().create(prepared)

    def write(self, values):
        immutable = {"template_id", "version", "layout_json", "workflow_json", "content_hash"}
        if immutable.intersection(values):
            raise AccessError("Approved template content is immutable.")
        return super().write(values)


class CcFieldDefinition(models.Model):
    _name = "cc.field.definition"
    _description = "Campaign Field Definition"
    _order = "template_version_id, sequence, key"

    template_version_id = fields.Many2one(
        "cc.campaign.template.version", required=True, ondelete="restrict", index=True
    )
    key = fields.Char(required=True, index=True)
    label = fields.Char(required=True)
    field_type = fields.Selection(
        [
            ("text", "Text"),
            ("integer", "Integer"),
            ("decimal", "Decimal"),
            ("boolean", "Boolean"),
            ("date", "Date"),
            ("datetime", "Datetime"),
            ("selection", "Selection"),
        ],
        required=True,
    )
    required = fields.Boolean(default=False)
    masked = fields.Boolean(default=False)
    visible_to_agent = fields.Boolean(default=True)
    reportable = fields.Boolean(default=False)
    validation_json = fields.Json(default=dict)
    options_json = fields.Json(default=list)
    sequence = fields.Integer(default=10)
    _key_unique = models.Constraint(
        "unique(template_version_id, key)",
        "Field keys must be unique within a template version.",
    )

    @api.constrains("key", "options_json", "field_type")
    def _check_definition(self):
        for field in self:
            if (
                not field.key
                or not field.key.replace("_", "").isalnum()
                or field.key[0].isdigit()
            ):
                raise ValidationError("Field keys must be safe snake-case identifiers.")
            if field.field_type == "selection" and not isinstance(field.options_json, list):
                raise ValidationError("Selection fields require an options list.")


class CcProfileFieldValue(models.Model):
    _name = "cc.profile.field.value"
    _description = "Typed Campaign Profile Field Value"
    _inherit = "cc.campaign.scoped.mixin"

    profile_id = fields.Many2one(
        "cc.customer.profile", required=True, ondelete="cascade", index=True
    )
    definition_id = fields.Many2one(
        "cc.field.definition", required=True, ondelete="restrict", index=True
    )
    value_text = fields.Text()
    value_integer = fields.Integer()
    value_decimal = fields.Float()
    value_boolean = fields.Boolean()
    value_date = fields.Date()
    value_datetime = fields.Datetime()
    value_json = fields.Json()
    _profile_field_unique = models.Constraint(
        "unique(profile_id, definition_id)",
        "A profile may have one value per field definition.",
    )

    @api.constrains("profile_id", "definition_id")
    def _check_scope(self):
        for value in self:
            if value.profile_id.campaign_id != value.campaign_id:
                raise ValidationError("Profile field value must match profile campaign.")


class CcWorkspaceSection(models.Model):
    _name = "cc.workspace.section"
    _description = "Campaign Workspace Section"
    _inherit = "cc.campaign.scoped.mixin"
    _order = "campaign_id, sequence, id"

    name = fields.Char(required=True)
    code = fields.Char(required=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    item_ids = fields.One2many("cc.workspace.item", "section_id")
    _code_unique = models.Constraint(
        "unique(campaign_id, code)", "Workspace section codes must be unique per campaign."
    )


class CcWorkspaceItem(models.Model):
    _name = "cc.workspace.item"
    _description = "Campaign Workspace Item"
    _inherit = "cc.campaign.scoped.mixin"
    _order = "section_id, sequence, id"

    section_id = fields.Many2one(
        "cc.workspace.section", required=True, ondelete="cascade", index=True
    )
    name = fields.Char(required=True)
    item_type = fields.Selection(
        [("field", "Field"), ("script", "Script"), ("queue", "Queue"), ("action", "Action")],
        required=True,
    )
    reference_key = fields.Char(required=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True, required=True)
    visible_to_agent = fields.Boolean(default=True)
    required = fields.Boolean(default=False)

    @api.constrains("section_id", "campaign_id")
    def _check_section_scope(self):
        for item in self:
            if item.section_id.campaign_id != item.campaign_id:
                raise ValidationError("Workspace item must match section campaign.")


class CcIntegrationBinding(models.Model):
    _name = "cc.integration.binding"
    _description = "Allowlisted Integration Binding"
    _inherit = "cc.campaign.scoped.mixin"

    name = fields.Char(required=True)
    adapter = fields.Selection(
        [
            ("middleware", "Middleware"),
            ("keycloak", "Keycloak"),
            ("vicidial", "VICIdial"),
            ("klyrow", "Klyrow"),
            ("telnexa", "Telnexa"),
            ("n8n", "n8n"),
        ],
        required=True,
    )
    operation = fields.Char(required=True)
    enabled = fields.Boolean(default=False)
    secret_reference = fields.Char(
        help="Reference only; credentials are stored outside Odoo."
    )
    allowlist_json = fields.Json(default=list)
    _operation_unique = models.Constraint(
        "unique(campaign_id, adapter, operation)",
        "Integration operations must be unique per campaign.",
    )

    @api.constrains("secret_reference")
    def _check_secret_reference(self):
        for binding in self:
            if binding.secret_reference and any(
                token in binding.secret_reference.lower()
                for token in ("password", "secret=", "token=", "private_key")
            ):
                raise ValidationError("Store only an external secret reference.")


class CcCampaignConfigurationVersion(models.Model):
    """Immutable campaign-specific configuration snapshot.

    This owns the Odoo *desired configuration* only. It is explicitly not a
    provider/provisioning manifest: per the locked design specification
    (docs/superpowers/specs/2026-09-15-odoo-unified-campaign-control-plane-design.md,
    section 8.3), Middleware alone owns the canonical provisioning manifest.
    Versions are created by the campaign lifecycle (validate/rollback) or by a
    global administrator preparing a reviewed configuration; their content
    never changes once written.
    """

    _name = "cc.campaign.configuration.version"
    _description = "Immutable Campaign Configuration Version"
    _order = "campaign_id, version desc"

    campaign_id = fields.Many2one("cc.campaign", required=True, ondelete="restrict", index=True)
    business_unit_id = fields.Many2one(
        related="campaign_id.cc_business_unit_id", store=True, readonly=True, index=True
    )
    template_version_id = fields.Many2one(
        "cc.campaign.template.version", ondelete="restrict"
    )
    version = fields.Integer(required=True, readonly=True, index=True)
    state = fields.Selection(
        [
            ("draft", "Draft"),
            ("in_review", "In Review"),
            ("approved", "Approved"),
            ("published", "Published"),
            ("blocked", "Blocked"),
            ("superseded", "Superseded"),
            ("rolled_back", "Rolled Back"),
        ],
        required=True,
        default="draft",
        readonly=True,
        index=True,
    )
    configuration_json = fields.Json(required=True, readonly=True)
    configuration_hash = fields.Char(required=True, size=64, readonly=True, index=True)
    validation_errors_json = fields.Json(readonly=True)
    predecessor_id = fields.Many2one(
        "cc.campaign.configuration.version", readonly=True, ondelete="restrict"
    )
    source_version_id = fields.Many2one(
        "cc.campaign.configuration.version",
        readonly=True,
        ondelete="restrict",
        help="Historical version whose configuration this rollback version reproduces.",
    )
    design_revision_id = fields.Many2one(
        "call.center.campaign.design.revision",
        readonly=True,
        ondelete="restrict",
        help="Link to the existing returned immutable Middleware design-revision evidence.",
    )
    created_by = fields.Many2one(
        "res.users", required=True, readonly=True, default=lambda self: self.env.user
    )
    approved_by = fields.Many2one("res.users", readonly=True)
    approved_at = fields.Datetime(readonly=True)
    published_by = fields.Many2one("res.users", readonly=True)
    published_at = fields.Datetime(readonly=True)
    _version_unique = models.Constraint(
        "unique(campaign_id, version)", "Configuration versions must be unique."
    )
    _version_positive = models.Constraint(
        "check(version > 0)", "Configuration versions must be positive."
    )
    _hash_format = models.Constraint(
        "check(length(configuration_hash) = 64)", "Configuration hashes must be SHA-256."
    )

    @api.constrains("campaign_id", "template_version_id")
    def _check_template_campaign(self):
        for configuration in self:
            if (
                configuration.template_version_id
                and configuration.campaign_id.template_id
                and configuration.template_version_id.template_id
                != configuration.campaign_id.template_id
            ):
                raise ValidationError(
                    "Configuration template version must match campaign template."
                )

    @api.model_create_multi
    def create(self, values_list):
        prepared = []
        for values in values_list:
            values = dict(values)
            document = values.get("configuration_json") or {}
            values["configuration_hash"] = hashlib.sha256(
                _canonical_json(document).encode()
            ).hexdigest()
            prepared.append(values)
        return super().create(prepared)

    def write(self, values):
        if set(values).intersection(
            {
                "campaign_id",
                "template_version_id",
                "version",
                "configuration_json",
                "configuration_hash",
                "validation_errors_json",
                "predecessor_id",
                "source_version_id",
            }
        ):
            raise AccessError("Configuration version content is immutable.")
        if (
            "state" in values
            and self.env.context.get("_cc_configuration_capability")
            is not CONFIGURATION_CAPABILITY
            and not self.env.user.has_group(
                "codestra_cc_security.group_cc_global_administrator"
            )
        ):
            raise AccessError("Configuration version state is lifecycle controlled.")
        return super().write(values)

    def unlink(self):
        raise AccessError("Configuration versions are immutable evidence.")

    @api.model
    def _create_internal(self, values):
        return self.sudo().with_context(
            _cc_configuration_capability=CONFIGURATION_CAPABILITY
        ).create(values)

    def _control_write(self, values):
        return self.sudo().with_context(
            _cc_configuration_capability=CONFIGURATION_CAPABILITY
        ).write(values)

    def action_publish(self):
        """Publish a reviewed configuration version as the current campaign
        configuration, superseding any prior published version. Provider-facing
        state is unchanged until the lifecycle provisions it."""
        for configuration in self:
            if configuration.state != "approved":
                raise ValidationError("Only an approved configuration version may be published.")
            previous = self.search(
                [
                    ("campaign_id", "=", configuration.campaign_id.id),
                    ("state", "=", "published"),
                    ("id", "!=", configuration.id),
                ]
            )
            previous._control_write({"state": "superseded"})
            configuration._control_write(
                {
                    "state": "published",
                    "published_by": self.env.user.id,
                    "published_at": fields.Datetime.now(),
                }
            )
            configuration.campaign_id._control_write(
                {"configuration_version_id": configuration.id}
            )


class CcCampaignControlExtension(models.Model):
    _inherit = "cc.campaign"

    application_id = fields.Many2one("cc.application", ondelete="restrict", index=True)
    template_id = fields.Many2one("cc.campaign.template", ondelete="restrict", index=True)
    template_version_id = fields.Many2one(
        "cc.campaign.template.version", ondelete="restrict", index=True
    )
    configuration_version_id = fields.Many2one(
        "cc.campaign.configuration.version",
        ondelete="restrict",
        index=True,
        copy=False,
        readonly=True,
    )

    @api.constrains("template_id", "template_version_id", "application_id")
    def _check_template_scope(self):
        for campaign in self:
            if campaign.template_version_id and campaign.template_id != campaign.template_version_id.template_id:
                raise ValidationError("Campaign template version must belong to campaign template.")
            if campaign.template_id and campaign.application_id != campaign.template_id.application_id:
                raise ValidationError("Campaign application must match template application.")

