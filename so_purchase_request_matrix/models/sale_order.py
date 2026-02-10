from odoo import api, fields, models, _
from odoo.exceptions import UserError


class SaleOrder(models.Model):
    _inherit = "sale.order"

    so_purchase_request_id = fields.Many2one("so.purchase.request", string="Purchase Request", readonly=True, copy=False)

    def action_open_purchase_request(self):
        self.ensure_one()
        if not self.so_purchase_request_id:
            raise UserError(_("No Purchase Request linked to this Sales Order."))
        return {
            "type": "ir.actions.act_window",
            "name": _("Purchase Request"),
            "res_model": "so.purchase.request",
            "view_mode": "form",
            "res_id": self.so_purchase_request_id.id,
        }

    def action_create_purchase_request_wizard(self):
        self.ensure_one()
        if self.so_purchase_request_id:
            return self.action_open_purchase_request()
        return {
            "type": "ir.actions.act_window",
            "name": _("Create Purchase Request"),
            "res_model": "so.create.purchase.request.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {"default_sale_order_id": self.id},
        }

