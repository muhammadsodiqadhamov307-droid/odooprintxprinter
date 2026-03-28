/** @odoo-module **/

/**
 * patch_printer.js
 * Intercepts PoS receipt and preparation-ticket prints and mirrors them
 * into /pos/add_print_job for pickup by the local Windows print agent.
 *
 * Targets: Odoo 19, OWL-based PoS
 */

import { patch } from '@web/core/utils/patch';
import { PosStore } from '@point_of_sale/app/services/pos_store';

function firstNonEmpty(...values) {
    for (const value of values) {
        if (value === null || value === undefined) {
            continue;
        }
        const text = String(value).trim();
        if (text) {
            return text;
        }
    }
    return '';
}

function resolvePrinterName(printer, fallback = 'Kitchen') {
    return firstNonEmpty(
        printer?.name,
        printer?.config?.name,
        printer?.config_id?.name,
        fallback
    );
}

function resolveTableLabel(data, order) {
    const table = order?.table_id || order?.table || order?.tableId || order?.getTable?.();
    return firstNonEmpty(
        data?.table_name,
        data?.table,
        data?.table_number,
        data?.table_id?.table_number,
        data?.table_id?.name,
        data?.order?.table_name,
        data?.order?.table,
        data?.order?.table_number,
        data?.order?.table_id?.table_number,
        data?.order?.table_id?.name,
        table?.table_number,
        table?.name,
        order?.table_name,
        order?.table_number
    );
}

function resolveOrderLabel(data, order) {
    return firstNonEmpty(
        data?.name,
        data?.trackingNumber,
        data?.tracking_number,
        data?.order_name,
        data?.order?.name,
        data?.order?.trackingNumber,
        data?.order?.tracking_number,
        order?.name,
        order?.trackingNumber,
        order?.tracking_number,
        order?.uid
    );
}

function normalizeQty(value) {
    if (value === null || value === undefined) {
        return 1;
    }
    if (typeof value === 'number' && Number.isFinite(value)) {
        return value;
    }
    const text = String(value).trim();
    if (!text) {
        return 1;
    }
    const parsed = Number(text.replace(',', '.'));
    return Number.isFinite(parsed) ? parsed : 1;
}

function normalizeLine(line) {
    const src = line && typeof line === 'object' ? line : {};
    return {
        ...src,
        qty: normalizeQty(
            src.qty ?? src.quantity ?? src.qty_done ?? src.count ?? src.new_qty ?? src.newQty ?? src.delta ?? src.amount
        ),
    };
}

function normalizeLineList(value) {
    if (Array.isArray(value)) {
        return value.map(normalizeLine);
    }
    if (value && typeof value === 'object') {
        return Object.values(value).map(normalizeLine);
    }
    return [];
}

function normalizeChanges(changes) {
    const src = changes && typeof changes === 'object' ? changes : {};
    const cancelled = normalizeLineList(src.cancelled).map((line) => ({
        ...line,
        qty: line.qty > 0 ? -line.qty : line.qty,
    }));
    return {
        ...src,
        new: normalizeLineList(src.new),
        cancelled,
        noteUpdate: normalizeLineList(src.noteUpdate),
        data: normalizeLineList(src.data),
    };
}

async function sendToPrintQueue(data, printerType = 'receipt', printerName = null) {
    // First try local push agent for immediate print. Fallback to Odoo queue.
    try {
        const res = await fetch('http://127.0.0.1:8899/print', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ data, printer_type: printerType, printer_name: printerName }),
        });
        if (res.ok) {
            const ok = await res.json();
            if (ok?.success) {
                return;
            }
        }
    } catch (err) {
        // ignore, will fallback
    }

    // Fallback to Odoo queue
    try {
        const response = await fetch('/pos/add_print_job', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({
                jsonrpc: '2.0',
                method: 'call',
                id: Date.now(),
                params: {
                    data: data,
                    printer_type: printerType,
                    printer_name: printerName,
                },
            }),
        });

        if (!response.ok) {
            console.error(
                `[PosCustomPrint] HTTP error sending to print queue: ${response.status}`
            );
            return;
        }

        const result = await response.json();
        if (result && result.result && result.result.success) {
            console.log(
                `[PosCustomPrint] Job queued OK: id=${result.result.job_id} type=${printerType}`
            );
        } else {
            const errMsg = result?.result?.error || 'Unknown error';
            console.error(`[PosCustomPrint] Server returned error: ${errMsg}`);
        }
    } catch (err) {
        console.error('[PosCustomPrint] Network error sending to print queue:', err);
    }
}

patch(PosStore.prototype, {
    async printReceipt({ basic = false, order = this.getOrder(), printBillActionTriggered = false } = {}) {
        try {
            if (order) {
                const tableLabel = firstNonEmpty(
                    order.table_id?.table_number,
                    order.table_id?.name,
                    order.table?.table_number,
                    order.table?.name
                );
                const printData = JSON.stringify({
                    type: 'receipt',
                    company_name: order.company?.name || 'Odoo POS',
                    order_name: order.name || '',
                    tracking_number: order.trackingNumber || '',
                    cashier: order.getCashierName ? order.getCashierName() : '',
                    date: order.date_order?.toISO ? order.date_order.toISO() : new Date().toISOString(),
                    table: tableLabel,
                    customer_count: order.customer_count || '',
                    printer_name: 'Receipt',
                    currency_symbol: order.currency?.symbol || '',
                    subtotal: order.priceExcl ?? order.priceIncl ?? 0,
                    tax: (order.priceIncl ?? 0) - (order.priceExcl ?? 0),
                    total: order.priceIncl ?? 0,
                    payments: (order.payment_ids || [])
                        .filter((payment) => !payment.is_change)
                        .map((payment) => ({
                            name: payment.payment_method_id?.name || 'Payment',
                            amount: payment.amount || 0,
                            amount_display:
                                (payment.currency &&
                                    this.env?.utils?.formatCurrency?.(payment.amount || 0, payment.currency.id)) ||
                                '',
                        })),
                lines: order.getOrderlines().map((line) => ({
                    name: line.getFullProductName ? line.getFullProductName() : '',
                    qty: line.getQuantity ? line.getQuantity() : line.quantity || 0,
                    price: line.priceIncl ?? line.getDisplayPrice?.() ?? 0,
                    price_display: line.currencyDisplayPrice || '',
                    unit_price_display:
                        line.currencyDisplayPriceUnit && line.product_id?.uom_id?.name
                            ? `${line.currencyDisplayPriceUnit} / ${line.product_id.uom_id.name}`
                            : '',
                })),
            });
            await sendToPrintQueue(printData, 'receipt', 'Receipt');
        }
    } catch (err) {
        console.error('[PosCustomPrint] Failed to queue receipt print:', err);
        return await super.printReceipt({ basic, order, printBillActionTriggered });
    }
        if (!printBillActionTriggered) {
            if (order) {
                const count = order.nb_print ? order.nb_print + 1 : 1;
                if (order.isSynced) {
                    const wasDirty = order.isDirty();
                    await this.data.write('pos.order', [order.id], { nb_print: count });
                    if (!wasDirty) {
                        order._dirty = false;
                    }
                } else {
                    order.nb_print = count;
                }
            }
        } else if (order && !order.nb_print) {
            order.nb_print = 0;
        }

        return { successful: true };
    },

    async printOrderChanges(data, printer) {
        try {
            const currentOrder = this.getOrder ? this.getOrder() : null;
            const printerName = resolvePrinterName(printer, 'Kitchen');
            const printData = JSON.stringify({
                type: 'kitchen',
                printer_name: printerName,
                table: resolveTableLabel(data, currentOrder),
                order: resolveOrderLabel(data, currentOrder),
                changes: normalizeChanges(data?.changes),
                date: new Date().toISOString(),
            });
            await sendToPrintQueue(printData, 'kitchen', printerName);
        } catch (err) {
            console.error('[PosCustomPrint] Failed to queue kitchen print:', err);
            return await super.printOrderChanges(data, printer);
        }
        return { successful: true };
    },
});
