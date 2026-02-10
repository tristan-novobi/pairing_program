/** @odoo-module **/
import { registry } from "@web/core/registry";
import { Component, useState, onWillStart, onMounted, onWillUnmount } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";

class PrqMatrix extends Component {
    setup() {
        this.orm = useService("orm");
        this.busService = useService("bus_service");
        this.notification = useService("notification");
        this.state = useState({
            loading: true,
            data: null,
            allocations: {}, // key: productId-vendorId -> {qty, price}
        });
        this._subscribeBus();
        onWillStart(async () => {
            await this.load();
        });
        onWillUnmount(() => {
            this._unsubscribeBus();
        });
    }
    get requestId() {
        return this.props.record.resId || (this.props.record.data && this.props.record.data.id);
    }
    async load() {
        const data = await this.orm.call("so.purchase.request", "prq_get_matrix_data", [this.requestId]);
        console.log("PRQ matrix: Loaded data", data);
        const allocs = {};
        for (const line of data.lines) {
            for (const a of (line.allocations || [])) {
                allocs[`${line.product_id}-${a.vendor_id}`] = { qty: a.qty_alloc, price: a.price_unit_alloc || 0 };
            }
        }
        const bestByProduct = {};
        for (const line of data.lines) {
            let bestVendorId = null;
            let bestPrice = Infinity;
            for (const v of data.vendors) {
                const cell = (line.quotes || {})[v.id] || (line.quotes || {})[String(v.id)];
                if (cell && typeof cell.normalized_price_unit === "number" && cell.normalized_price_unit > 0) {
                    if (cell.normalized_price_unit < bestPrice) {
                        bestPrice = cell.normalized_price_unit;
                        bestVendorId = v.id;
                    }
                }
            }
            if (bestVendorId !== null) {
                bestByProduct[line.product_id] = bestVendorId;
            }
        }
        this.state.data = data;
        this.state.allocations = allocs;
        this.state.bestByProduct = bestByProduct;
        this.state.loading = false;
    }
    _subscribeBus() {
        try {
            this.busService.addChannel("so_prq_matrix");
            console.log("PRQ matrix: Bus Service subscribed to so_prq_matrix");
        } catch (e) {
            console.warn("PRQ matrix: Bus Service subscribe failed", e);
        }
    }
    _unsubscribeBus() {
        try {
            console.log("PRQ matrix: Bus Service unsubscribed from so_prq_matrix");
            if (this.busService.deleteChannel) {
                this.busService.deleteChannel("so_prq_matrix");
            } else if (this.busService.removeChannel) {
                this.busService.removeChannel("so_prq_matrix");
            }
        } catch {
            // ignore
        }
    }
    _getAllocKey(productId, vendorId) {
        return `${productId}-${vendorId}`;
    }
    onChangeQty(ev, productId, vendorId) {
        const key = this._getAllocKey(productId, vendorId);
        const value = parseFloat(ev.target.value || "0") || 0;
        const current = this.state.allocations[key] || { qty: 0, price: 0 };
        // If split mode and price not set yet, default from RFQ when user assigns qty
        if (this.state.data?.approval_split_by_vendor && value > 0 && (!current.price || Number(current.price) === 0)) {
            const line = (this.state.data.lines || []).find((l) => l.product_id === productId);
            const cell = (line?.quotes || {})[vendorId] || (line?.quotes || {})[String(vendorId)] || {};
            const defaultPrice = Number(cell.price_unit) || Number(cell.normalized_price_unit) || 0;
            this.state.allocations[key] = { qty: value, price: defaultPrice };
        } else {
            this.state.allocations[key] = { ...current, qty: value };
        }
        if (!this.state.data?.approval_split_by_vendor) {
            for (const v of (this.state.data?.vendors || [])) {
                if (v.id === vendorId) continue;
                const k = this._getAllocKey(productId, v.id);
                const cur = this.state.allocations[k];
                if (cur && cur.qty) {
                    this.state.allocations[k] = { ...cur, qty: 0 };
                }
            }
        }
    }
    onChangePrice(ev, productId, vendorId) {
        const key = this._getAllocKey(productId, vendorId);
        const value = parseFloat(ev.target.value || "0") || 0;
        const current = this.state.allocations[key] || { qty: 0, price: 0 };
        this.state.allocations[key] = { ...current, price: value };
    }
    onCellClick(line, vendor) {
        if (this.state.data?.approval_split_by_vendor) {
            return;
        }
        const full = Number(line.qty_request) || 0;
        this.onChangeQty({ target: { value: full } }, line.product_id, vendor.id);
    }
    async toggleSplit(ev) {
        const enabled = !!ev.target.checked;
        try {
            await this.orm.call("so.purchase.request", "write", [[this.requestId], { approval_split_by_vendor: enabled }]);
            this.state.data.approval_split_by_vendor = enabled;
            this.notification.add(_t("Mode updated"), { type: "success" });
        } catch (e) {
            this.notification.add(e.message || _t("Failed to update mode"), { type: "danger" });
            ev.target.checked = !enabled;
            throw e;
        }
    }
    async save() {
        const payload = [];
        const data = this.state.data;
        for (const line of data.lines) {
            const fullQty = Number(line.qty_request) || 0;
            const quotes = line.quotes || {};
            if (!data.approval_split_by_vendor) {
                let selectedVendorId = null;
                for (const v of data.vendors) {
                    const key = this._getAllocKey(line.product_id, v.id);
                    const a = this.state.allocations[key];
                    if (a && Number(a.qty) > 0) {
                        selectedVendorId = v.id;
                        break;
                    }
                }
                if (selectedVendorId === null) {
                    selectedVendorId = this.state.bestByProduct?.[line.product_id] ?? (data.vendors[0] && data.vendors[0].id);
                }
                for (const v of data.vendors) {
                    const key = this._getAllocKey(line.product_id, v.id);
                    const a = this.state.allocations[key] || {};
                    const cell = quotes[v.id] || quotes[String(v.id)] || {};
                    const price = Number(a.price) || Number(cell.normalized_price_unit) || Number(cell.price_unit) || 0;
                    payload.push({
                        product_id: line.product_id,
                        vendor_id: v.id,
                        qty_alloc: v.id === selectedVendorId ? fullQty : 0,
                        price_unit_alloc: v.id === selectedVendorId ? price : 0,
                        taxes_id: [],
                    });
                }
            } else {
                let total = 0;
                const perVendor = {};
                for (const v of data.vendors) {
                    const key = this._getAllocKey(line.product_id, v.id);
                    const a = this.state.allocations[key] || {};
                    const qty = Number(a.qty) || 0;
                    const cell = quotes[v.id] || quotes[String(v.id)] || {};
                    // Default unit price from RFQ first, then normalized as fallback
                    const price = Number(a.price) || Number(cell.price_unit) || Number(cell.normalized_price_unit) || 0;
                    perVendor[v.id] = { qty, price };
                    total += qty;
                }
                if (total < fullQty) {
                    const rest = fullQty - total;
                    const fallback = this.state.bestByProduct?.[line.product_id] ?? (data.vendors[0] && data.vendors[0].id);
                    if (fallback) {
                        perVendor[fallback].qty += rest;
                    }
                }
                for (const v of data.vendors) {
                    const pv = perVendor[v.id] || { qty: 0, price: 0 };
                    payload.push({
                        product_id: line.product_id,
                        vendor_id: v.id,
                        qty_alloc: pv.qty,
                        price_unit_alloc: pv.price,
                        taxes_id: [],
                    });
                }
            }
        }
        try {
            await this.orm.call("so.purchase.request", "prq_save_allocations", [this.requestId, payload]);
            this.notification.add(_t("Allocations saved"), { type: "success" });
            await this.load();
        } catch (e) {
            this.notification.add(e.message || "Error", { type: "danger" });
            throw e;
        }
    }
}
PrqMatrix.template = "so_purchase_request_matrix.PrqMatrix";

const PrqMatrixField = {
    component: PrqMatrix,
    supportedTypes: ["text", "char"],
    displayName: "PRQ Matrix",
    extractProps: ({ record, value, name }) => ({ record, value, name }),
    isEmpty: () => false,
};

registry.category("fields").add("prq_matrix", PrqMatrixField);

