from odoo import fields, models


class PurchaseOrder(models.Model):
    _inherit = "purchase.order"

    so_request_id = fields.Many2one("so.purchase.request", string="Purchase Request", ondelete="set null", index=True)
    so_from_request = fields.Boolean(string="Created From Purchase Request", default=False)
    is_final_po = fields.Boolean(string="Final PO", default=False)
    active = fields.Boolean(string="Active", default=True)
