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

const TAKEOUT_NAME_KEY = '__posCustomPrintTakeoutName';
let activePosStore = null;
let takeoutObserverStarted = false;
let lastTakeoutName = '';
let lastTakeoutOrderKey = '';
let dailySalesPrintInFlight = false;

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

function isPlaceholderLabel(value) {
    const text = String(value ?? '').trim().toLowerCase();
    return ['n/a', 'na', '-', '--', 'none', 'null'].includes(text);
}

function looksLikeSyntheticTakeoutLabel(value) {
    const text = String(value ?? '').trim();
    return /^\d+\s*x$/i.test(text);
}

function resolvePrinterName(printer, fallback = 'Kitchen') {
    return firstNonEmpty(
        printer?.name,
        printer?.config?.name,
        printer?.config_id?.name,
        fallback
    );
}

function isVisibleElement(node) {
    if (!(node instanceof HTMLElement)) {
        return false;
    }
    return !!(node.offsetParent || node.getClientRects().length);
}

function normalizedNodeText(node) {
    return String(node?.textContent || '').replace(/\s+/g, ' ').trim();
}

function currentOrder() {
    return activePosStore?.getOrder ? activePosStore.getOrder() : null;
}

function asInt(value) {
    const parsed = Number.parseInt(value, 10);
    return Number.isFinite(parsed) ? parsed : null;
}

function resolveSessionId(pos) {
    return (
        asInt(pos?.pos_session?.id) ??
        asInt(pos?.pos_session?.[0]) ??
        asInt(pos?.session?.id) ??
        asInt(pos?.session?.[0]) ??
        asInt(pos?.config?.current_session_id?.id) ??
        asInt(pos?.config?.current_session_id?.[0]) ??
        asInt(pos?.config?.current_session_id)
    );
}

function resolveConfigId(pos) {
    return (
        asInt(pos?.config?.id) ??
        asInt(pos?.config_id?.id) ??
        asInt(pos?.config_id?.[0])
    );
}

function orderCacheKey(order) {
    return firstNonEmpty(order?.uid, order?.uuid, order?.cid, order?.name, order?.id);
}

function setTakeoutName(order, rawName) {
    const name = firstNonEmpty(rawName);
    if (!order || !name) {
        return;
    }
    lastTakeoutName = name;
    lastTakeoutOrderKey = orderCacheKey(order);
    order[TAKEOUT_NAME_KEY] = name;
    order.takeout_name = name;
}

function clearTakeoutName(order) {
    if (order) {
        delete order[TAKEOUT_NAME_KEY];
        delete order.takeout_name;
    }
    lastTakeoutName = '';
    lastTakeoutOrderKey = '';
}

function resolveTakeoutName(data, order) {
    const partner = order?.get_partner?.() || order?.partner || order?.partner_id;
    const currentKey = orderCacheKey(order);
    const scopedLastTakeoutName =
        !lastTakeoutOrderKey
            ? lastTakeoutName
            : (order && currentKey && currentKey === lastTakeoutOrderKey ? lastTakeoutName : '');
    return firstNonEmpty(
        data?.headerData?.takeout_name,
        data?.headerData?.pickup_name,
        data?.headerData?.customer_name,
        data?.headerData?.customerName,
        data?.takeout_name,
        data?.pickup_name,
        data?.customer_name,
        data?.customerName,
        data?.booking_name,
        data?.bookingName,
        data?.open_tab_name,
        data?.openTabName,
        data?.service_name,
        data?.serviceName,
        data?.order?.takeout_name,
        data?.order?.pickup_name,
        data?.order?.customer_name,
        data?.order?.customerName,
        data?.orderData?.takeout_name,
        data?.orderData?.pickup_name,
        data?.orderData?.customer_name,
        data?.orderData?.customerName,
        data?.orderChange?.takeout_name,
        data?.orderChange?.pickup_name,
        data?.orderChange?.customer_name,
        data?.orderChange?.customerName,
        order?.[TAKEOUT_NAME_KEY],
        order?.takeout_name,
        order?.pickup_name,
        order?.customer_name,
        order?.customerName,
        order?.booking_name,
        order?.bookingName,
        order?.open_tab_name,
        order?.openTabName,
        order?.uiState?.orderName,
        order?.uiState?.name,
        partner?.name,
        scopedLastTakeoutName
    );
}

function rememberTakeoutNameFromDialog(dialog) {
    const inputs = Array.from(
        dialog.querySelectorAll('input[type="text"], input:not([type]), textarea')
    ).filter((node) => isVisibleElement(node));
    if (!inputs.length) {
        return;
    }
    const activeInput = inputs.find((input) => input === document.activeElement);
    const value = firstNonEmpty(
        activeInput?.value,
        ...inputs.map((input) => input.value)
    );
    if (!value) {
        return;
    }
    lastTakeoutName = value;
    setTakeoutName(currentOrder(), value);
}

function autoAdvanceTakeoutPresetDialog(dialog) {
    if (dialog.dataset.posCustomPrintTakeoutHandled === '1') {
        return;
    }
    const dialogText = normalizedNodeText(dialog);
    if (!/select a preset/i.test(dialogText) || !/takeout/i.test(dialogText)) {
        return;
    }
    const slotButtons = Array.from(dialog.querySelectorAll('button')).filter((button) => {
        return isVisibleElement(button) && /^\d{1,2}:\d{2}$/.test(normalizedNodeText(button));
    });
    const continueButton = Array.from(dialog.querySelectorAll('button')).find((button) => {
        return isVisibleElement(button) && /continue/i.test(normalizedNodeText(button));
    });
    if (!slotButtons.length || !continueButton) {
        return;
    }
    dialog.dataset.posCustomPrintTakeoutHandled = '1';
    window.setTimeout(() => {
        try {
            const slot =
                slotButtons.find((button) => !button.disabled && button.getAttribute('aria-disabled') !== 'true') ||
                slotButtons[0];
            slot?.click();
            window.setTimeout(() => continueButton.click(), 120);
        } catch (error) {
            console.warn('[PosCustomPrint] Could not auto-advance takeout preset dialog', error);
        }
    }, 50);
}

function scanTakeoutDialogs() {
    if (typeof document === 'undefined') {
        return;
    }
    const dialogs = Array.from(
        document.querySelectorAll('[role="dialog"], .modal, .popup, .dialog')
    ).filter((node) => isVisibleElement(node));
    for (const dialog of dialogs) {
        rememberTakeoutNameFromDialog(dialog);
        autoAdvanceTakeoutPresetDialog(dialog);
    }
}

function ensureTakeoutObserver() {
    if (takeoutObserverStarted || typeof document === 'undefined') {
        return;
    }
    if (!document.body) {
        window.addEventListener('DOMContentLoaded', ensureTakeoutObserver, { once: true });
        return;
    }
    takeoutObserverStarted = true;
    const observer = new MutationObserver(() => scanTakeoutDialogs());
    observer.observe(document.body, {
        childList: true,
        subtree: true,
        attributes: true,
        attributeFilter: ['class', 'style', 'disabled', 'aria-disabled'],
    });
    document.addEventListener('click', () => window.setTimeout(scanTakeoutDialogs, 30), true);
    document.addEventListener('input', () => window.setTimeout(scanTakeoutDialogs, 30), true);
    window.setTimeout(scanTakeoutDialogs, 0);
}

ensureTakeoutObserver();

function isDailySalesButton(button) {
    if (!(button instanceof HTMLElement) || !isVisibleElement(button)) {
        return false;
    }
    const text = normalizedNodeText(button).toLowerCase();
    if (!text) {
        return false;
    }
    if (!/(kundalik\s*sotuv|daily\s*sale(s)?|sales\s*details)/i.test(text)) {
        return false;
    }
    const dialog = button.closest('[role="dialog"], .modal, .popup, .dialog');
    if (!dialog) {
        return false;
    }
    const dialogText = normalizedNodeText(dialog).toLowerCase();
    return /(registrni\s*yopish|close\s*register|closing\s*control)/i.test(dialogText);
}

async function fetchDailySalesReportData() {
    const pos = activePosStore;
    if (!pos) {
        throw new Error('POS store is not ready yet');
    }
    const sessionId = resolveSessionId(pos);
    const configId = resolveConfigId(pos);
    const response = await fetch('/pos/daily_sales_report', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            jsonrpc: '2.0',
            method: 'call',
            id: Date.now(),
            params: {
                session_id: sessionId,
                config_id: configId,
            },
        }),
    });
    if (!response.ok) {
        throw new Error(`Daily sales request failed with HTTP ${response.status}`);
    }
    const result = await response.json();
    if (!result?.result?.success || !result?.result?.data) {
        throw new Error(result?.result?.error || 'Could not build daily sales report');
    }
    return result.result.data;
}

async function handleDailySalesClick(event) {
    const button = event.target?.closest?.('button');
    if (!button || !isDailySalesButton(button)) {
        return;
    }
    event.preventDefault();
    event.stopPropagation();
    event.stopImmediatePropagation();
    if (dailySalesPrintInFlight) {
        return;
    }
    dailySalesPrintInFlight = true;
    button.disabled = true;
    try {
        const data = await fetchDailySalesReportData();
        await sendToPrintQueue(JSON.stringify(data), 'receipt', 'Receipt');
    } catch (error) {
        console.error('[PosCustomPrint] Failed to print daily sales report:', error);
        window.alert(`Daily sales print failed: ${error?.message || error}`);
    } finally {
        button.disabled = false;
        dailySalesPrintInFlight = false;
    }
}

if (typeof document !== 'undefined') {
    document.addEventListener('click', handleDailySalesClick, true);
}

function resolveTableLabel(data, order) {
    const tableSource = order?.getTable?.() ?? order?.table_id ?? order?.tableId ?? order?.table;
    const tableObject = tableSource && typeof tableSource === 'object' ? tableSource : null;
    const tablePrimitive =
        tableSource !== null && tableSource !== undefined && typeof tableSource !== 'object'
            ? String(tableSource).trim()
            : '';
    const rawTable = firstNonEmpty(
        data?.headerData?.table_name,
        data?.headerData?.table,
        data?.headerData?.table_number,
        data?.headerData?.table_id?.table_number,
        data?.headerData?.table_id?.name,
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
        data?.orderData?.table_name,
        data?.orderData?.table,
        data?.orderData?.table_number,
        data?.orderData?.table_id?.table_number,
        data?.orderData?.table_id?.name,
        data?.orderChange?.table_name,
        data?.orderChange?.table,
        data?.orderChange?.table_number,
        data?.orderChange?.table_id?.table_number,
        data?.orderChange?.table_id?.name,
        tableObject?.table_number,
        tableObject?.name,
        tablePrimitive,
        order?.table_name,
        order?.table_number
    );
    const takeoutName = resolveTakeoutName(data, order);
    if (takeoutName && (isPlaceholderLabel(rawTable) || looksLikeSyntheticTakeoutLabel(rawTable))) {
        return firstNonEmpty(takeoutName, rawTable);
    }
    return firstNonEmpty(rawTable, takeoutName);
}

function resolveOrderLabel(data, order) {
    return firstNonEmpty(
        data?.headerData?.name,
        data?.headerData?.trackingNumber,
        data?.headerData?.tracking_number,
        data?.headerData?.order_name,
        data?.name,
        data?.trackingNumber,
        data?.tracking_number,
        data?.order_name,
        data?.order?.name,
        data?.order?.trackingNumber,
        data?.order?.tracking_number,
        data?.orderData?.name,
        data?.orderData?.trackingNumber,
        data?.orderData?.tracking_number,
        data?.orderData?.order_name,
        data?.orderChange?.name,
        data?.orderChange?.trackingNumber,
        data?.orderChange?.tracking_number,
        data?.orderChange?.order_name,
        order?.name,
        order?.trackingNumber,
        order?.tracking_number,
        order?.uid
    );
}

function parseQty(value, fallback = 1) {
    if (value === null || value === undefined) {
        return fallback;
    }
    if (typeof value === 'number' && Number.isFinite(value)) {
        return value;
    }
    const text = String(value).trim();
    if (!text) {
        return fallback;
    }
    const parsed = Number(text.replace(',', '.'));
    return Number.isFinite(parsed) ? parsed : fallback;
}

function asChangeList(value) {
    if (Array.isArray(value)) {
        return value;
    }
    if (value instanceof Set || value instanceof Map) {
        return Array.from(value.values());
    }
    if (value && typeof value === 'object') {
        return Object.values(value);
    }
    return [];
}

function resolveReceiptChangeSection(receiptData, changes = null) {
    const receiptLines = receiptData?.changes?.data;
    if (changes && receiptLines === changes.new) {
        return 'new';
    }
    if (changes && receiptLines === changes.cancelled) {
        return 'cancelled';
    }
    if (changes && receiptLines === changes.noteUpdate) {
        return 'noteUpdate';
    }

    const explicitSection = firstNonEmpty(
        receiptData?.changes?.section,
        receiptData?.changes?.change_section
    )
        .replace(/\s+/g, '')
        .toLowerCase();

    if (explicitSection === 'new' || explicitSection === 'cancelled' || explicitSection === 'canceled') {
        return explicitSection === 'canceled' ? 'cancelled' : explicitSection;
    }
    if (explicitSection === 'noteupdate' || explicitSection === 'note_update') {
        return 'noteUpdate';
    }

    const title = firstNonEmpty(receiptData?.changes?.title).replace(/\s+/g, '').toLowerCase();
    if (title === 'cancelled' || title === 'canceled') {
        return 'cancelled';
    }
    if (title === 'new') {
        return 'new';
    }
    if (title === 'noteupdate') {
        return 'noteUpdate';
    }
    return 'new';
}

function buildSignedChangeLine(line, section) {
    const src = line && typeof line === 'object' ? line : {};
    const rawQty =
        src.qty ?? src.quantity ?? src.qty_done ?? src.count ?? src.amount ?? 1;
    const magnitude = Math.abs(parseQty(rawQty, 1));
    const signedQty = section === 'cancelled' ? -magnitude : magnitude;
    return {
        ...src,
        qty: signedQty,
        quantity: signedQty,
        change_section: section,
    };
}

function withSignedChangePayload(receiptData, changes = null) {
    if (!receiptData?.changes || typeof receiptData.changes !== 'object') {
        return receiptData;
    }

    const section = resolveReceiptChangeSection(receiptData, changes);
    const signedLines = Array.isArray(receiptData.changes.signed_lines)
        ? receiptData.changes.signed_lines
        : asChangeList(receiptData.changes.data).map((line) => buildSignedChangeLine(line, section));

    return {
        ...receiptData,
        changes: {
            ...receiptData.changes,
            section,
            signed_lines: signedLines,
        },
    };
}

const PRIORITY_QTY_KEYS = [
    'delta',
    'qty_delta',
    'qtyDelta',
    'change',
    'change_qty',
    'changeQty',
    'difference',
    'diff',
    'removed_qty',
    'removedQty',
    'cancelled_qty',
    'cancelledQty',
    'canceled_qty',
    'canceledQty',
    'decrease_qty',
    'decreaseQty',
    'decreased_qty',
    'decreasedQty',
];

const FALLBACK_QTY_KEYS = [
    'qty',
    'quantity',
    'qty_done',
    'count',
    'new_qty',
    'newQty',
    'amount',
];

const NEGATIVE_CHANGE_MARKERS = new Set([
    'cancelled',
    'canceled',
    'removed',
    'remove',
    'delete',
    'deleted',
    'cxl',
    'minus',
    'negative',
    'decrease',
    'decreased',
    'reduce',
    'reduced',
    'less',
    'decrement',
    'decremented',
    'subtract',
    'subtracted',
]);

const NEGATIVE_CHANGE_TOKENS = [
    'cancel',
    'cxl',
    'remove',
    'delete',
    'minus',
    'negative',
    'decrease',
    'reduce',
    'decrement',
    'subtract',
    'less',
];

const SEMANTIC_QTY_KEY_TOKENS = [
    'qty',
    'quantity',
    'count',
    'delta',
    'change',
    'diff',
    'difference',
    'decrease',
    'decrement',
    'reduce',
    'remove',
    'cancel',
];

const OLD_QTY_TOKENS = ['old', 'prev', 'previous', 'before', 'from'];
const NEW_QTY_TOKENS = ['new', 'current', 'after', 'to'];

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

function collectQtyCandidates(src) {
    if (!src || typeof src !== 'object') {
        return [];
    }
    const candidates = [];
    for (const key of [...PRIORITY_QTY_KEYS, ...FALLBACK_QTY_KEYS]) {
        if (!(key in src) || src[key] === null || src[key] === undefined) {
            continue;
        }
        candidates.push({ key, value: normalizeQty(src[key]) });
    }
    return candidates;
}

function collectSemanticQtyCandidates(src) {
    if (!src || typeof src !== 'object') {
        return [];
    }
    const candidates = [];
    for (const [key, rawValue] of Object.entries(src)) {
        if (rawValue === null || rawValue === undefined) {
            continue;
        }
        const normalizedKey = String(key).toLowerCase();
        if (
            !SEMANTIC_QTY_KEY_TOKENS.some((token) => normalizedKey.includes(token)) ||
            ['price', 'cost', 'tax', 'total', 'subtotal', 'discount'].some((token) => normalizedKey.includes(token))
        ) {
            continue;
        }
        const value = normalizeQty(rawValue);
        if (Number.isFinite(value)) {
            candidates.push({ key, value });
        }
    }
    return candidates;
}

function impliedQtyDelta(src) {
    if (!src || typeof src !== 'object') {
        return null;
    }
    let oldValue = null;
    let newValue = null;
    for (const [key, rawValue] of Object.entries(src)) {
        const normalizedKey = String(key).toLowerCase();
        if (
            !SEMANTIC_QTY_KEY_TOKENS.some((token) => normalizedKey.includes(token)) ||
            ['price', 'cost', 'tax', 'total', 'subtotal', 'discount'].some((token) => normalizedKey.includes(token))
        ) {
            continue;
        }
        const value = normalizeQty(rawValue);
        if (!Number.isFinite(value)) {
            continue;
        }
        if (OLD_QTY_TOKENS.some((token) => normalizedKey.includes(token))) {
            oldValue = value;
        }
        if (NEW_QTY_TOKENS.some((token) => normalizedKey.includes(token))) {
            newValue = value;
        }
    }
    if (oldValue === null || newValue === null) {
        return null;
    }
    const delta = newValue - oldValue;
    return delta === 0 ? null : delta;
}

function extractKitchenQty(src) {
    const candidates = [...collectQtyCandidates(src), ...collectSemanticQtyCandidates(src)];
    if (!candidates.length) {
        const delta = impliedQtyDelta(src);
        return delta === null ? 1 : delta;
    }
    const explicitNegative = candidates.find((candidate) => candidate.value < 0);
    if (explicitNegative) {
        return explicitNegative.value;
    }
    const delta = impliedQtyDelta(src);
    if (delta !== null) {
        return delta;
    }
    const priorityValue = candidates.find((candidate) => PRIORITY_QTY_KEYS.includes(candidate.key));
    if (priorityValue) {
        return priorityValue.value;
    }
    return candidates[0].value;
}

function lineMarker(src) {
    if (!src || typeof src !== 'object') {
        return '';
    }
    return firstNonEmpty(
        src.section,
        src.state,
        src.status,
        src.change_type,
        src.changeType,
        src.type,
        src.action,
        src.operation,
        src.kind,
        src.difference_type,
        src.differenceType,
        src.delta_type,
        src.deltaType
    ).toLowerCase();
}

function normalizeLine(line, sectionName = '') {
    const src = line && typeof line === 'object' ? line : {};
    return {
        ...src,
        qty: extractKitchenQty(src),
    };
}

function normalizeLineList(value, sectionName = '') {
    if (Array.isArray(value)) {
        return value.map((line) => normalizeLine(line, sectionName));
    }
    if (value instanceof Set) {
        return Array.from(value.values()).map((line) => normalizeLine(line, sectionName));
    }
    if (value instanceof Map) {
        return Array.from(value.values()).map((line) => normalizeLine(line, sectionName));
    }
    if (value && typeof value === 'object') {
        return Object.values(value).map((line) => normalizeLine(line, sectionName));
    }
    return [];
}

function normalizeChanges(changes) {
    const src = changes && typeof changes === 'object' ? changes : {};
    const normalized = { ...src };
    
    for (const [sectionName, sectionValue] of Object.entries(src)) {
        normalized[sectionName] = normalizeLineList(sectionValue, sectionName);
    }
    
    // Unify all cancellation/reduction aliases into 'cancelled'
    const cancelled = []
        .concat(normalized.cancelled || [])
        .concat(normalized.removed || [])
        .concat(normalized.remove || [])
        .concat(normalized.decrease || [])
        .concat(normalized.decreased || [])
        .concat(normalized.reduce || [])
        .concat(normalized.reduced || []);
    
    delete normalized.removed;
    delete normalized.remove;
    delete normalized.decrease;
    delete normalized.decreased;
    delete normalized.reduce;
    delete normalized.reduced;

    normalized.new = normalized.new || [];
    normalized.cancelled = cancelled;
    normalized.noteUpdate = normalized.noteUpdate || [];
    normalized.data = normalized.data || [];
    
    return normalized;
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
    getOrder(...args) {
        activePosStore = this;
        return super.getOrder(...args);
    },

    async generateReceiptsDataToPrint(orderData, changes, orderChange) {
        const receiptsData = await super.generateReceiptsDataToPrint(orderData, changes, orderChange);
        return receiptsData.map((receiptData) => withSignedChangePayload(receiptData, changes));
    },

    async printReceipt({ basic = false, order = this.getOrder(), printBillActionTriggered = false } = {}) {
        try {
            activePosStore = this;
            if (order) {
                const takeoutName = resolveTakeoutName({}, order);
                if (takeoutName) {
                    setTakeoutName(order, takeoutName);
                }
                const tableLabel = resolveTableLabel({}, order);
                const printData = JSON.stringify({
                    type: 'receipt',
                    company_name: order.company?.name || 'Odoo POS',
                    order_name: order.name || '',
                    tracking_number: order.trackingNumber || '',
                    cashier: order.getCashierName ? order.getCashierName() : '',
                    date: order.date_order?.toISO ? order.date_order.toISO() : new Date().toISOString(),
                    table: tableLabel,
                    takeout_name: takeoutName,
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
                    unit_price_display: line.currencyDisplayPriceUnit || '',
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
            activePosStore = this;
            const currentOrder = this.getOrder ? this.getOrder() : null;
            const printerName = resolvePrinterName(printer, 'Kitchen');
            const normalizedData = withSignedChangePayload(data);
            const takeoutName = resolveTakeoutName(normalizedData, currentOrder);
            if (takeoutName) {
                setTakeoutName(currentOrder, takeoutName);
            }
            const rawChanges =
                normalizedData?.changes && typeof normalizedData.changes === 'object'
                    ? normalizedData.changes
                    : normalizedData;
            const printData = JSON.stringify({
                type: 'kitchen',
                printer_name: printerName,
                table: resolveTableLabel(normalizedData, currentOrder),
                takeout_name: takeoutName,
                order: resolveOrderLabel(normalizedData, currentOrder),
                waiter: currentOrder?.getCashierName ? currentOrder.getCashierName() : '',
                cashier: currentOrder?.getCashierName ? currentOrder.getCashierName() : '',
                changes: rawChanges,
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
