/** @odoo-module **/

import { Component } from "@odoo/owl";

/** Where every conversation currently sits, as one proportional bar. */
export class MixBar extends Component {
    static template = "ibq_whatsapp_helpdesk.MixBar";
    static props = {
        segments: { type: Array },
    };

    get total() {
        return this.props.segments.reduce((sum, s) => sum + s.value, 0);
    }

    get rows() {
        const total = this.total;
        return this.props.segments.map((segment) => ({
            ...segment,
            percent: total ? Math.round((segment.value * 100) / total) : 0,
            // Keep a hairline visible for non-zero-but-tiny segments.
            width: total ? Math.max((segment.value * 100) / total, segment.value ? 1.5 : 0) : 0,
        }));
    }
}
