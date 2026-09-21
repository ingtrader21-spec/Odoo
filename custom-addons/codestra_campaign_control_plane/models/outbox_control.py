"""Bind outbox delivery outcomes to campaign provisioning runs.

Middleware pulls control commands through the governed claim/acknowledge/fail
API. Acknowledgement only records intake on the run; dead-lettering fails it.
Campaign desired/effective state is never derived from delivery outcomes.
"""

from odoo import models


class CodestraIntegrationOutboxControl(models.Model):
    _inherit = "codestra.runtime.integration.outbox"

    def _control_campaign(self):
        self.ensure_one()
        if self.aggregate_type != "cc.campaign":
            return self.env["cc.campaign"]
        return (
            self.env["cc.campaign"]
            .sudo()
            .with_context(active_test=False)
            .browse(self.aggregate_record_id)
            .exists()
        )

    def _on_acknowledged(self):
        super()._on_acknowledged()
        campaign = self._control_campaign()
        if campaign:
            campaign._control_delivery_acknowledged(self.event_uuid)

    def _on_dead_letter(self):
        super()._on_dead_letter()
        campaign = self._control_campaign()
        if campaign:
            campaign._control_delivery_dead_lettered(self.event_uuid)
