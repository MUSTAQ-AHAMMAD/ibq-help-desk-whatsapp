/** @odoo-module **/

import { Component } from "@odoo/owl";

/**
 * One headline number with its period-over-period movement.
 *
 * `lowerIsBetter` flips the good/bad colouring, because a falling response
 * time is an improvement while a falling message count is not.
 */
export class StatTile extends Component {
    static template = "ibq_whatsapp_helpdesk.StatTile";
    static props = {
        label: { type: String },
        value: { type: [String, Number] },
        delta: { optional: true },
        lowerIsBetter: { type: Boolean, optional: true },
        hint: { type: String, optional: true },
        emphasis: { type: Boolean, optional: true },
    };

    get hasDelta() {
        return this.props.delta !== null && this.props.delta !== undefined;
    }

    get deltaLabel() {
        const d = this.props.delta;
        return `${d > 0 ? "+" : ""}${d}%`;
    }

    get deltaClass() {
        const d = this.props.delta;
        if (!d) {
            return "o_wa_delta--flat";
        }
        const good = this.props.lowerIsBetter ? d < 0 : d > 0;
        return good ? "o_wa_delta--up" : "o_wa_delta--down";
    }
}
