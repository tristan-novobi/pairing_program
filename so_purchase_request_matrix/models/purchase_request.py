from odoo import api, fields, models, _
from odoo.exceptions import ValidationError, UserError


class PurchaseRequest(models.Model):
    _name = "so.purchase.request"
    _description = "Purchase Request"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "id desc"

    name = fields.Char(string="Request Number", tracking=True, default="/", copy=False, readonly=True)
    sale_order_id = fields.Many2one("sale.order", string="Sales Order", required=True, ondelete="restrict", index=True)
    state = fields.Selection([
        ("draft", "Draft"),
        ("vendors_selected", "Vendors Selected"),
        ("rfqs_created", "RFQs Created"),
        ("waiting_approval", "Waiting Approval"),
        ("approved", "Approved"),
        ("po_created", "PO Created"),
        ("cancel", "Cancelled"),
    ], string="Status", default="draft", tracking=True)
    company_id = fields.Many2one("res.company", string="Company", required=True, default=lambda self: self.env.company, index=True)
    currency_id = fields.Many2one("res.currency", string="Company Currency", related="company_id.currency_id", store=True, readonly=True)
    vendor_ids = fields.Many2many("res.partner", "so_prq_vendor_rel", "request_id", "vendor_id",
                                  domain=[("supplier_rank", ">", 0)],
                                  string="Vendors")
    line_ids = fields.One2many("so.purchase.request.line", "request_id", string="Lines")
    rfq_ids = fields.One2many("purchase.order", "so_request_id", string="RFQs")
    not_final_po_ids = fields.One2many("purchase.order", "so_request_id", string="Not Final POs", domain=[("is_final_po", "=", False)])
    final_po_ids = fields.One2many("purchase.order", "so_request_id", string="Final POs", domain=[("is_final_po", "=", True)])
    quote_line_ids = fields.One2many("so.purchase.request.quote.line", "request_id", string="Quote Lines")
    allocation_ids = fields.One2many("so.purchase.request.allocation", "request_id", string="Allocations")
    approval_split_by_vendor = fields.Boolean(string="Allow Split By Vendor", help="If enabled, approver can split quantities per product across vendors.")
    note = fields.Text(string="Internal Note")
    comparison_matrix = fields.Text(string="Comparison Matrix (UI)", compute="_compute_matrix_placeholder")

    _sql_constraints = [
        ("sale_order_unique", "unique(sale_order_id)", "A Purchase Request already exists for this Sales Order."),
    ]

    def write(self, vals):
        res = super().write(vals)
        self._bus_send_matrix_update()
        return res

    @api.depends_context("id")
    def _compute_matrix_placeholder(self):
        for rec in self:
            rec.comparison_matrix = "matrix"

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get("name") or vals.get("name") == "/":
                vals["name"] = self.env["ir.sequence"].next_by_code("so.purchase.request") or "/"
        return super().create(vals_list)

    # ---------- Actions ----------
    def action_select_vendors(self):
        self.ensure_one()
        if not self.vendor_ids:
            raise UserError(_("Please select at least one vendor."))
        self.state = "vendors_selected"
        return True

    def _prepare_rfq_vals(self, vendor):
        self.ensure_one()
        return {
            "partner_id": vendor.id,
            "origin": self.name,
            "company_id": self.company_id.id,
            "currency_id": vendor.property_purchase_currency_id.id or self.currency_id.id,
            "so_request_id": self.id,
            "so_from_request": True,
        }

    def _prepare_rfq_line_vals(self, line, order):
        product = line.product_id
        uom = line.product_uom_id or product.uom_po_id
        qty = line.qty_request
        return {
            "order_id": order.id,
            "product_id": product.id,
            "name": line.description or product.display_name,
            "product_uom": uom.id,
            "product_qty": qty,
            "price_unit": 0.0,
            "date_planned": fields.Date.context_today(self),
            "taxes_id": [(6, 0, product.supplier_taxes_id.ids)],
        }

    def action_create_rfqs(self):
        for request in self:
            if not request.vendor_ids:
                raise UserError(_("Please select at least one vendor."))
            created_orders = self.env["purchase.order"]
            for vendor in request.vendor_ids:
                po_vals = request._prepare_rfq_vals(vendor)
                po = self.env["purchase.order"].create(po_vals)
                # po.active = False
                for line in request.line_ids:
                    self.env["purchase.order.line"].create(request._prepare_rfq_line_vals(line, po))
                created_orders |= po
            request.state = "rfqs_created"
        return True

    def action_sync_quotes(self):
        for request in self:
            for po in request.rfq_ids:
                for pol in po.order_line:
                    # upsert quote line by (request, vendor, product)
                    ql = self.env["so.purchase.request.quote.line"].search([
                        ("request_id", "=", request.id),
                        ("vendor_id", "=", po.partner_id.id),
                        ("product_id", "=", pol.product_id.id),
                    ], limit=1)
                    vals = {
                        "request_id": request.id,
                        "vendor_id": po.partner_id.id,
                        "product_id": pol.product_id.id,
                        "product_uom_id": pol.product_uom.id,
                        "qty_quote": pol.product_qty,
                        "price_unit_quote": pol.price_unit,
                        "currency_id": po.currency_id.id,
                        "taxes_id": [(6, 0, pol.taxes_id.ids)],
                        "lead_time_days": 0,
                        "validity_date": po.date_order and po.date_order.date(),
                        "vendor_note": po.notes or False,
                        "source_rfq_id": po.id,
                    }
                    if ql:
                        ql.write(vals)
                    else:
                        self.env["so.purchase.request.quote.line"].create(vals)
        
        self._bus_send_matrix_update()
        return True

    def action_submit_for_approval(self):
        for request in self:
            if not request.quote_line_ids:
                raise UserError(_("Please sync quotations from RFQs before submitting for approval."))
            request.state = "waiting_approval"
            # Schedule a basic activity for Purchase Manager group users on the document
            manager_group = self.env.ref("purchase.group_purchase_manager", raise_if_not_found=False)
            if manager_group:
                manager_users = manager_group.users
                for user in manager_users:
                    self.activity_schedule(
                        "mail.mail_activity_data_todo", user_id=user.id,
                        note=_("Purchase Request %s waiting for approval.") % request.name,
                    )
        return True

    def _validate_allocations(self):
        for request in self:
            # sums per product must equal requested qty
            by_product = {}
            for alloc in request.allocation_ids:
                by_product.setdefault(alloc.product_id.id, 0.0)
                by_product[alloc.product_id.id] += alloc.qty_alloc
            for line in request.line_ids:
                total = by_product.get(line.product_id.id, 0.0)
                if abs(total - line.qty_request) > 1e-6:
                    raise ValidationError(_(
                        "Allocation error for product '%s': allocated quantity %.2f must equal requested quantity %.2f."
                    ) % (line.product_id.display_name, total, line.qty_request))
        return True

    def action_approve(self):
        self._validate_allocations()
        self.write({"state": "approved"})
        return True

    def _prepare_po_from_alloc_vendor(self, vendor):
        self.ensure_one()
        currency = vendor.property_purchase_currency_id or self.company_id.currency_id
        return {
            "partner_id": vendor.id,
            "origin": self.name,
            "company_id": self.company_id.id,
            "currency_id": currency.id,
            "so_request_id": self.id,
            "so_from_request": True,
        }

    def _convert_price_to_currency(self, price, from_currency, to_currency, date):
        return from_currency._convert(price, to_currency, self.company_id, date or fields.Date.context_today(self))

    def action_create_pos(self):
        PurchaseOrder = self.env["purchase.order"]
        PurchaseOrderLine = self.env["purchase.order.line"]
        for request in self:
            if request.state not in ("approved", "waiting_approval"):
                raise UserError(_("Allocations must be approved first."))
            request._validate_allocations()
            # group allocations by vendor
            allocs_by_vendor = {}
            for alloc in request.allocation_ids:
                allocs_by_vendor.setdefault(alloc.vendor_id, self.env["so.purchase.request.allocation"])
                allocs_by_vendor[alloc.vendor_id] |= alloc
            for vendor, allocs in allocs_by_vendor.items():
                po_vals = request._prepare_po_from_alloc_vendor(vendor)
                po_vals["is_final_po"] = True
                po = PurchaseOrder.create(po_vals)
                fpos = po.fiscal_position_id
                for alloc in allocs:
                    po_uom = alloc.product_id.uom_po_id
                    qty_po = alloc.product_uom_id._compute_quantity(alloc.qty_alloc, po_uom, rounding_method="HALF-UP")
                    # price conversion: allocation currency -> PO currency
                    # Determine base price: explicit override or best available normalized quote
                    alloc_price = alloc.price_unit_alloc or 0.0
                    if not alloc_price:
                        ql = alloc.quote_line_id or self.env["so.purchase.request.quote.line"].search([
                            ("request_id", "=", request.id),
                            ("vendor_id", "=", alloc.vendor_id.id),
                            ("product_id", "=", alloc.product_id.id),
                        ], limit=1)
                        if ql:
                            alloc_price = ql.normalized_price_unit
                    price_in_po_currency = self._convert_price_to_currency(
                        alloc_price, alloc.currency_id, po.currency_id, po.date_order
                    )
                    taxes = alloc.taxes_id
                    if not taxes:
                        ql_taxes = self.env["so.purchase.request.quote.line"].search([
                            ("request_id", "=", request.id),
                            ("vendor_id", "=", alloc.vendor_id.id),
                            ("product_id", "=", alloc.product_id.id),
                        ], limit=1).taxes_id
                        taxes = ql_taxes or alloc.product_id.supplier_taxes_id
                    if fpos:
                        taxes = fpos.map_tax(taxes)
                    PurchaseOrderLine.create({
                        "order_id": po.id,
                        "product_id": alloc.product_id.id,
                        "name": alloc.product_id.display_name,
                        "product_uom": po_uom.id,
                        "product_qty": qty_po,
                        "price_unit": price_in_po_currency,
                        "date_planned": fields.Date.context_today(self),
                        "taxes_id": [(6, 0, taxes.ids)],
                    })
            request.state = "po_created"
        return True

    def _bus_send_matrix_update(self):
        self.env["bus.bus"]._sendone(f"so_prq_matrix", "matrix_update", {
            "type": "matrix_update",
        })

    @api.model
    def prq_get_matrix_data(self, request_id):
        request = self.browse(request_id)
        request.check_access_rights("read")
        request.check_access_rule("read")
        vendors = []
        for v in request.vendor_ids:
            rfqs_vendor = request.rfq_ids.filtered(lambda po: po.partner_id.id == v.id)
            planned_dates = rfqs_vendor.mapped("order_line.date_planned")
            date_planned = False
            if planned_dates:
                min_dt = min(planned_dates)
                try:
                    date_planned = fields.Datetime.to_string(min_dt)
                except Exception:
                    try:
                        date_planned = fields.Date.to_string(min_dt)
                    except Exception:
                        date_planned = str(min_dt)
            payment_term = False
            if rfqs_vendor:
                for po in rfqs_vendor:
                    if po.payment_term_id:
                        payment_term = po.payment_term_id.name
                        break
            date_planned = date_planned.split(" ")[0] if date_planned else _("No information available")
            vendors.append({
                "id": v.id,
                "name": v.display_name,
                "date_planned": date_planned,
                "payment_term": payment_term or _("No information available"),
            })
        lines = []
        for rl in request.line_ids:
            row = {
                "product_id": rl.product_id.id,
                "product_name": rl.product_id.display_name,
                "qty_request": rl.qty_request,
                "uom_name": rl.product_uom_id.name,
                "quotes": {},
            }
            for q in request.quote_line_ids.filtered(lambda q: q.product_id.id == rl.product_id.id):
                row["quotes"][str(q.vendor_id.id)] = {
                    "price_unit": q.price_unit_quote,
                    "currency": q.currency_id.display_name,
                    "normalized_price_unit": q.normalized_price_unit,
                }
            # current allocations
            allocs = request.allocation_ids.filtered(lambda a: a.product_id.id == rl.product_id.id)
            row["allocations"] = [{
                "vendor_id": a.vendor_id.id,
                "qty_alloc": a.qty_alloc,
                "price_unit_alloc": a.price_unit_alloc or 0.0,
            } for a in allocs]
            lines.append(row)
        return {
            "id": request.id,
            "name": request.name,
            "approval_split_by_vendor": request.approval_split_by_vendor,
            "company_currency": request.currency_id.display_name,
            "vendors": vendors,
            "lines": lines,
        }

    @api.model
    def prq_save_allocations(self, request_id, allocations):
        """allocations: list of dicts {product_id, vendor_id, qty_alloc, price_unit_alloc, taxes_id}"""
        request = self.browse(request_id)
        request.check_access_rights("write")
        request.check_access_rule("write")
        Allocation = self.env["so.purchase.request.allocation"]
        if not request.approval_split_by_vendor:
            existing = Allocation.search([
                ("request_id", "=", request.id),
            ], limit=1)
            if existing:
                existing.unlink()
        print("allocations", allocations)
        for alloc in allocations:
            existing = Allocation.search([
                ("request_id", "=", request.id),
                ("product_id", "=", alloc.get("product_id")),
                ("vendor_id", "=", alloc.get("vendor_id")),
            ], limit=1)
            if existing:
                existing.unlink()
            if alloc.get("qty_alloc", 0.0) > 0:
                vals = {
                    "request_id": request.id,
                    "product_id": alloc.get("product_id"),
                    "vendor_id": alloc.get("vendor_id"),
                    "qty_alloc": alloc.get("qty_alloc", 0.0),
                    "price_unit_alloc": alloc.get("price_unit_alloc", 0.0),
                    "currency_id": request.currency_id.id,
                    "taxes_id": [(6, 0, alloc.get("taxes_id", []))],
                }
                Allocation.create(vals)
        # validate totals
        request._validate_allocations()
        return True


class PurchaseRequestLine(models.Model):
    _name = "so.purchase.request.line"
    _description = "Purchase Request Line"

    request_id = fields.Many2one("so.purchase.request", string="Request", required=True, ondelete="cascade", index=True)
    product_id = fields.Many2one("product.product", string="Product", required=True, ondelete="restrict", index=True)
    product_uom_id = fields.Many2one("uom.uom", string="UoM", required=True)
    qty_request = fields.Float(string="Requested Quantity", required=True, digits="Product Unit of Measure")
    description = fields.Text(string="Description")

    @api.onchange("product_id")
    def _onchange_product_id(self):
        for rec in self:
            if rec.product_id and not rec.product_uom_id:
                rec.product_uom_id = rec.product_id.uom_po_id or rec.product_id.uom_id


class PurchaseRequestQuoteLine(models.Model):
    _name = "so.purchase.request.quote.line"
    _description = "Purchase Request Quote Line"
    _rec_name = "display_name"

    request_id = fields.Many2one("so.purchase.request", string="Request", required=True, ondelete="cascade", index=True)
    vendor_id = fields.Many2one("res.partner", string="Vendor", required=True, ondelete="restrict", index=True)
    product_id = fields.Many2one("product.product", string="Product", required=True, ondelete="restrict", index=True)
    product_uom_id = fields.Many2one("uom.uom", string="Vendor UoM", required=True)
    qty_quote = fields.Float(string="Quoted Qty", digits="Product Unit of Measure")
    price_unit_quote = fields.Monetary(string="Unit Price", currency_field="currency_id")
    currency_id = fields.Many2one("res.currency", string="Currency", required=True)
    taxes_id = fields.Many2many("account.tax", string="Taxes")
    lead_time_days = fields.Integer(string="Lead Time (days)")
    validity_date = fields.Date(string="Validity")
    vendor_note = fields.Text(string="Vendor Note")
    source_rfq_id = fields.Many2one("purchase.order", string="Source RFQ", ondelete="set null")
    display_name = fields.Char(string="Name", compute="_compute_display_name", store=False)

    # normalized for comparison
    normalized_price_unit = fields.Monetary(string="Price (Company Currency)", currency_field="company_currency_id", compute="_compute_normalized", store=True)
    normalized_qty = fields.Float(string="Qty (Request UoM)", compute="_compute_normalized", store=True, digits="Product Unit of Measure")
    company_currency_id = fields.Many2one("res.currency", string="Company Currency", related="request_id.currency_id", store=True, readonly=True)

    _sql_constraints = [
        ("unique_vendor_product", "unique(request_id,vendor_id,product_id)", "Each vendor can have only one quote per product in a request."),
    ]

    @api.depends("vendor_id", "product_id")
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = "%s - %s" % (rec.vendor_id.display_name or "", rec.product_id.display_name or "")

    @api.depends(
        "price_unit_quote",
        "currency_id",
        "product_uom_id",
        "qty_quote",
        "product_id",
        "request_id",
        "request_id.company_id",
        "request_id.company_id.currency_id",
        "request_id.line_ids.product_uom_id",
    )
    def _compute_normalized(self):
        for rec in self:
            # convert qty to request line uom
            request_line = self.env["so.purchase.request.line"].search([
                ("request_id", "=", rec.request_id.id),
                ("product_id", "=", rec.product_id.id),
            ], limit=1)
            req_uom = request_line.product_uom_id if request_line else rec.product_id.uom_po_id
            normalized_qty = rec.product_uom_id._compute_quantity(rec.qty_quote or 0.0, req_uom, rounding_method="HALF-UP") if rec.product_uom_id and req_uom else rec.qty_quote or 0.0
            # convert price to company currency
            price = rec.price_unit_quote or 0.0
            company_currency = rec.request_id.company_id.currency_id
            price_company = rec.currency_id._convert(price, company_currency, rec.request_id.company_id, fields.Date.context_today(self))
            rec.normalized_qty = normalized_qty
            rec.normalized_price_unit = price_company


class PurchaseRequestAllocation(models.Model):
    _name = "so.purchase.request.allocation"
    _description = "Purchase Request Allocation"

    request_id = fields.Many2one("so.purchase.request", string="Request", required=True, ondelete="cascade", index=True)
    product_id = fields.Many2one("product.product", string="Product", required=True, ondelete="restrict", index=True)
    vendor_id = fields.Many2one("res.partner", string="Vendor", required=True, ondelete="restrict", domain=[("supplier_rank", ">", 0)], index=True)
    qty_alloc = fields.Float(string="Allocated Qty", required=True, digits="Product Unit of Measure")
    price_unit_alloc = fields.Monetary(string="Allocated Unit Price", currency_field="currency_id",
                                       help="Override price for PO creation; falls back to best normalized quote if not set.")
    currency_id = fields.Many2one("res.currency", string="Currency", required=True, default=lambda self: self.env.company.currency_id.id)
    taxes_id = fields.Many2many("account.tax", string="Taxes")
    note_alloc = fields.Char(string="Reason/Note")
    quote_line_id = fields.Many2one("so.purchase.request.quote.line", string="Related Quote", ondelete="set null")
    product_uom_id = fields.Many2one("uom.uom", string="Request UoM", compute="_compute_request_uom", store=False)

    @api.constrains("qty_alloc")
    def _check_positive_qty(self):
        for rec in self:
            if rec.qty_alloc < 0:
                raise ValidationError(_("Allocated quantity must be positive."))

    def _compute_request_uom(self):
        for rec in self:
            req_line = self.env["so.purchase.request.line"].search([
                ("request_id", "=", rec.request_id.id),
                ("product_id", "=", rec.product_id.id),
            ], limit=1)
            rec.product_uom_id = req_line.product_uom_id or rec.product_id.uom_po_id
