from odoo import api, fields, models, _
from odoo.exceptions import UserError


class SoCreatePurchaseRequestWizard(models.TransientModel):
    _name = "so.create.purchase.request.wizard"
    _description = "Create Purchase Request from Sales Order"

    sale_order_id = fields.Many2one("sale.order", string="Sales Order", required=True)
    note = fields.Text(string="Note")

    def action_confirm(self):
        self.ensure_one()
        sale = self.sale_order_id
        if sale.so_purchase_request_id:
            raise UserError(_("A Purchase Request already exists for this Sales Order."))
        if not sale.order_line:
            raise UserError(_("No lines in Sales Order to create request from."))
        request = self.env["so.purchase.request"].create({
            "sale_order_id": sale.id,
            "note": self.note or False,
        })
        lines_vals = []
        for so_line in sale.order_line.filtered(lambda l: not l.display_type and l.product_id):
            uom = so_line.product_uom or so_line.product_id.uom_po_id
            lines_vals.append({
                "request_id": request.id,
                "product_id": so_line.product_id.id,
                "product_uom_id": uom.id,
                "qty_request": so_line.product_uom_qty,
                "description": so_line.name,
            })
        if lines_vals:
            self.env["so.purchase.request.line"].create(lines_vals)
        sale.so_purchase_request_id = request.id
