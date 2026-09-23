import hashlib
import hmac
import json
import time
import uuid

from odoo.tests import HttpCase, tagged


@tagged("post_install", "-at_install")
class TestMiddlewareCrmCustomerRecordsHttp(HttpCase):
    secret = "synthetic-middleware-crm-records-secret"
    tenant = "synthetic-records-tenant"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.unit = cls.env["call.center.business.unit"].create({
            "name": "Synthetic Records Unit",
            "code": f"MWREC-{uuid.uuid4().hex[:8]}",
            "company_id": cls.env.company.id,
        })
        service_group = cls.env.ref("codestra_middleware_bridge.group_codestra_crm_api")
        cls.service_user = cls.env["res.users"].create({
            "name": "Synthetic Middleware Records Service",
            "login": f"middleware-records-{uuid.uuid4()}@example.invalid",
            "company_id": cls.env.company.id,
            "company_ids": [(6, 0, cls.env.company.ids)],
            "call_center_business_unit_ids": [(6, 0, cls.unit.ids)],
            "call_center_default_business_unit_id": cls.unit.id,
            "call_center_primary_role": "integration_service",
            # cc.customer.profile/cc.helpdesk.* record rules gate non-admin
            # access through cc.campaign.membership (a separate, heavier grant
            # this fixture doesn't otherwise exercise); global-admin is used
            # here to reach the endpoints under test without standing up that
            # whole membership workflow. A real production credential for
            # this service would instead hold a proper campaign membership,
            # not this broad a grant -- noted as a test-fidelity tradeoff.
            "group_ids": [(6, 0, [
                cls.env.ref("base.group_user").id, service_group.id,
                cls.env.ref("codestra_cc_security.group_cc_global_administrator").id,
            ])],
        })
        params = cls.env["ir.config_parameter"].sudo()
        params.set_param("codestra.middleware.tenant_id", cls.tenant)
        params.set_param("codestra.middleware.inbound_hmac_secret", cls.secret)
        params.set_param("codestra.crm.tenant_ids", cls.tenant)
        params.set_param("codestra.crm.service_user_id", cls.service_user.id)
        cls.env["cc.business.unit"]._adopt_legacy_records()
        cls.canonical_unit = cls.env["cc.business.unit"].with_context(
            active_test=False
        ).search([("legacy_business_unit_id", "=", cls.unit.id)], limit=1)
        legacy_campaign = cls.env["call.center.campaign"].create({
            "name": "Synthetic Records Campaign",
            "code": f"MWRECCAMP-{uuid.uuid4().hex[:8].upper()}",
            "business_unit_id": cls.unit.id,
            "active": True,
        })
        cls.campaign = cls.env["cc.campaign"].with_context(
            active_test=False
        ).search([("legacy_campaign_id", "=", legacy_campaign.id)], limit=1)
        cls.partner = cls.env["res.partner"].create({"name": "Synthetic Customer Partner"})
        crm_service_user = cls.env["res.users"].create({
            "name": "Synthetic CRM Service Record Creator",
            "login": f"crm-service-{uuid.uuid4()}@example.invalid",
            "company_id": cls.env.company.id,
            "company_ids": [(6, 0, cls.env.company.ids)],
            "group_ids": [(6, 0, [
                cls.env.ref("base.group_user").id,
                cls.env.ref("codestra_cc_crm.group_cc_crm_service").id,
            ])],
        })
        cls.profile = cls.env["cc.customer.profile"].with_user(crm_service_user).create_from_partner(
            cls.partner, cls.campaign, integration_key=f"profile-{uuid.uuid4()}",
        )
        cls.queue = cls.env["cc.helpdesk.queue"].create({
            "name": "Synthetic Helpdesk Queue",
            "queue_key": f"queue-{uuid.uuid4().hex[:8]}",
            "campaign_id": cls.campaign.id,
        })
        # Ticket creation resolves an approved same-campaign/queue/priority SLA
        # policy server-side. The model enforces a real submit-then-approve
        # governance workflow (a sentinel-gated write() rejects direct state
        # changes even under sudo), requiring a distinct requester and a
        # distinct global-admin approver -- exercised for real here rather
        # than bypassed.
        # Both requester and approver need the global-administrator group:
        # the campaign-scope record rule only bypasses cc_allowed_campaign_ids
        # (a separate campaign-membership grant this fixture isn't otherwise
        # exercising) for global admins.
        sla_requester = cls.env["res.users"].create({
            "name": "Synthetic SLA Requester",
            "login": f"sla-requester-{uuid.uuid4()}@example.invalid",
            "company_id": cls.env.company.id,
            "company_ids": [(6, 0, cls.env.company.ids)],
            "group_ids": [(6, 0, [
                cls.env.ref("base.group_user").id,
                cls.env.ref("codestra_cc_security.group_cc_campaign_configuration_manager").id,
                cls.env.ref("codestra_cc_security.group_cc_global_administrator").id,
            ])],
        })
        sla_approver = cls.env["res.users"].create({
            "name": "Synthetic SLA Approver",
            "login": f"sla-approver-{uuid.uuid4()}@example.invalid",
            "company_id": cls.env.company.id,
            "company_ids": [(6, 0, cls.env.company.ids)],
            "group_ids": [(6, 0, [
                cls.env.ref("base.group_user").id,
                cls.env.ref("codestra_cc_security.group_cc_global_administrator").id,
            ])],
        })
        sla_policy = cls.env["cc.helpdesk.sla.policy"].with_user(sla_requester).create({
            "name": "Synthetic SLA Policy", "queue_id": cls.queue.id,
            "campaign_id": cls.campaign.id, "priority": "1",
            "source_ticket": f"sla-{uuid.uuid4()}",
        })
        sla_policy.with_user(sla_requester).action_submit()
        sla_policy.with_user(sla_approver).action_approve()

    def _signed(self, method, path, payload=None):
        raw = json.dumps(payload).encode() if payload is not None else b"{}"
        timestamp = str(int(time.time()))
        event_id = str(uuid.uuid4())
        correlation_id = f"correlation-{event_id}"
        idempotency_key = f"idempotency-{event_id}"
        canonical = b"\n".join((
            timestamp.encode(), event_id.encode(), method.encode(), path.encode(),
            self.tenant.encode(), correlation_id.encode(), idempotency_key.encode(), raw,
        ))
        signature = hmac.new(self.secret.encode(), canonical, hashlib.sha256).hexdigest()
        headers = {
            "Content-Type": "application/json",
            "X-Codestra-Timestamp": timestamp,
            "X-Codestra-Event-ID": event_id,
            "X-Codestra-Signature": f"sha256={signature}",
            "X-Tenant-ID": self.tenant,
            "X-Correlation-ID": correlation_id,
            "Idempotency-Key": idempotency_key,
        }
        url = self.base_url() + path
        return self.opener.request(method, url, data=raw, headers=headers, timeout=20)

    # -- customer profiles --

    def test_create_and_read_customer_profile(self):
        # A fresh partner: (partner, campaign) is unique per profile, and
        # self.partner/self.campaign are already consumed by self.profile.
        fresh_partner = self.env["res.partner"].create({"name": "Second Synthetic Partner"})
        response = self._signed("POST", "/codestra/middleware/v1/customer-profiles", {
            "name": "New Synthetic Customer", "partner_id": fresh_partner.id,
            "campaign_id": self.campaign.id, "integration_key": f"created-{uuid.uuid4()}",
        })
        self.assertEqual(response.status_code, 201, response.text)
        profile_id = response.json()["profile_id"]
        read = self._signed("GET", f"/codestra/middleware/v1/customer-profiles/{profile_id}")
        self.assertEqual(read.status_code, 200, read.text)
        self.assertEqual(read.json()["profile_id"], profile_id)

    def test_customer_profile_outside_unit_not_found(self):
        other_unit = self.env["call.center.business.unit"].create({
            "name": "Other Unit", "code": f"OTH-{uuid.uuid4().hex[:8]}", "company_id": self.env.company.id,
        })
        self.env["cc.business.unit"]._adopt_legacy_records()
        other_canonical = self.env["cc.business.unit"].with_context(active_test=False).search(
            [("legacy_business_unit_id", "=", other_unit.id)], limit=1
        )
        other_campaign_legacy = self.env["call.center.campaign"].create({
            "name": "Other Campaign", "code": f"OTHCAMP-{uuid.uuid4().hex[:8].upper()}",
            "business_unit_id": other_unit.id, "active": True,
        })
        other_campaign = self.env["cc.campaign"].with_context(active_test=False).search(
            [("legacy_campaign_id", "=", other_campaign_legacy.id)], limit=1
        )
        other_partner = self.env["res.partner"].create({"name": "Other Unit Partner"})
        crm_service_user = self.env["res.users"].create({
            "name": "Synthetic CRM Service Record Creator 2",
            "login": f"crm-service-2-{uuid.uuid4()}@example.invalid",
            "company_id": self.env.company.id,
            "company_ids": [(6, 0, self.env.company.ids)],
            "group_ids": [(6, 0, [
                self.env.ref("base.group_user").id,
                self.env.ref("codestra_cc_crm.group_cc_crm_service").id,
            ])],
        })
        other_profile = self.env["cc.customer.profile"].with_user(crm_service_user).create_from_partner(
            other_partner, other_campaign, integration_key=f"other-{uuid.uuid4()}",
        )
        response = self._signed("GET", f"/codestra/middleware/v1/customer-profiles/{other_profile.id}")
        self.assertEqual(response.status_code, 404, response.text)

    # -- notes --

    def test_create_and_list_note(self):
        create = self._signed("POST", f"/codestra/middleware/v1/customer-profiles/{self.profile.id}/notes", {
            "body": "Synthetic note body",
        })
        self.assertEqual(create.status_code, 201, create.text)
        listing = self._signed("GET", f"/codestra/middleware/v1/customer-profiles/{self.profile.id}/notes")
        self.assertEqual(listing.status_code, 200, listing.text)
        bodies = [item["body"] for item in listing.json()["items"]]
        self.assertTrue(any("Synthetic note body" in body for body in bodies))

    def test_update_note(self):
        create = self._signed("POST", f"/codestra/middleware/v1/customer-profiles/{self.profile.id}/notes", {
            "body": "Original body",
        })
        note_id = create.json()["note_id"]
        update = self._signed("PATCH", f"/codestra/middleware/v1/notes/{note_id}", {"body": "Updated body"})
        self.assertEqual(update.status_code, 200, update.text)
        self.assertIn("Updated body", update.json()["body"])

    # -- tasks --

    def test_create_and_complete_task(self):
        create = self._signed("POST", f"/codestra/middleware/v1/customer-profiles/{self.profile.id}/tasks", {
            "summary": "Synthetic follow-up task", "activity_type": "todo",
        })
        self.assertEqual(create.status_code, 201, create.text)
        task_id = create.json()["task_id"]
        listing = self._signed("GET", f"/codestra/middleware/v1/customer-profiles/{self.profile.id}/tasks")
        self.assertEqual(listing.status_code, 200, listing.text)
        self.assertTrue(any(item["task_id"] == task_id for item in listing.json()["items"]))
        complete = self._signed("POST", f"/codestra/middleware/v1/tasks/{task_id}/complete", {"feedback": "done"})
        self.assertEqual(complete.status_code, 200, complete.text)
        self.assertEqual(complete.json()["status"], "completed")


    def test_update_task_returns_parent_profile_for_deterministic_readback(self):
        create = self._signed("POST", f"/codestra/middleware/v1/customer-profiles/{self.profile.id}/tasks", {
            "summary": "Original task", "activity_type": "todo",
        })
        self.assertEqual(create.status_code, 201, create.text)
        task_id = create.json()["task_id"]
        update = self._signed("PATCH", f"/codestra/middleware/v1/tasks/{task_id}", {
            "summary": "Updated task",
        })
        self.assertEqual(update.status_code, 200, update.text)
        self.assertEqual(update.json()["task_id"], task_id)
        self.assertEqual(update.json()["profile_id"], self.profile.id)
        self.assertEqual(update.json()["summary"], "Updated task")

    # -- tickets --

    def test_create_and_read_ticket(self):
        create = self._signed("POST", "/codestra/middleware/v1/tickets", {
            "queue_id": self.queue.id, "customer_profile_id": self.profile.id,
            "subject": "Synthetic billing question", "category": "billing",
            "priority": "1", "severity": "low", "integration_key": f"ticket-{uuid.uuid4()}",
        })
        self.assertEqual(create.status_code, 201, create.text)
        ticket_id = create.json()["ticket_id"]
        read = self._signed("GET", f"/codestra/middleware/v1/tickets/{ticket_id}")
        self.assertEqual(read.status_code, 200, read.text)
        self.assertEqual(read.json()["subject"], "Synthetic billing question")

    def test_ticket_update(self):
        create = self._signed("POST", "/codestra/middleware/v1/tickets", {
            "queue_id": self.queue.id, "customer_profile_id": self.profile.id,
            "subject": "Synthetic technical issue", "category": "technical",
            "priority": "1", "severity": "low", "integration_key": f"ticket-{uuid.uuid4()}",
        })
        ticket_id = create.json()["ticket_id"]
        update = self._signed("PATCH", f"/codestra/middleware/v1/tickets/{ticket_id}", {
            "resolution": "Synthetic resolution notes",
        })
        self.assertEqual(update.status_code, 200, update.text)

    # -- opportunities (crm.lead) --

    def test_create_and_list_crm_lead(self):
        external_id = f"opp-{uuid.uuid4()}"
        create = self._signed("POST", "/codestra/middleware/v1/crm/leads", {
            "name": "Synthetic Opportunity", "external_id": external_id,
            "middleware_id": f"mw-{uuid.uuid4()}",
        })
        self.assertEqual(create.status_code, 201, create.text)
        listing = self._signed("GET", "/codestra/middleware/v1/crm/leads")
        self.assertEqual(listing.status_code, 200, listing.text)
        external_ids = [item["external_id"] for item in listing.json()["items"]]
        self.assertIn(external_id, external_ids)
