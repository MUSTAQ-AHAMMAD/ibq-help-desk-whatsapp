/** @odoo-module **/

import { Component } from "@odoo/owl";

/**
 * Inbound volume by weekday and hour.
 *
 * Answers the one staffing question every support lead has: when is the queue
 * actually busy? Rendered as plain elements rather than SVG so each cell can
 * carry a native tooltip and a focus ring.
 */
export class Heatmap extends Component {
    static template = "ibq_whatsapp_helpdesk.Heatmap";
    static props = {
        data: { type: Object },
    };

    /** Hour labels, thinned so they stay readable on a narrow panel. */
    get hours() {
        return Array.from({ length: 24 }, (_unused, hour) => ({
            hour,
            label: String(hour).padStart(2, "0"),
            show: hour % 3 === 0,
        }));
    }

    get rows() {
        const { days, grid, peak } = this.props.data;
        return days.map((day, index) => ({
            day,
            cells: grid[index].map((value, hour) => ({
                hour,
                value,
                // Five steps rather than a continuous ramp: readable at a
                // glance, and every non-zero cell stays visible.
                level: value === 0 ? 0 : Math.max(1, Math.ceil((value / (peak || 1)) * 4)),
                title: `${day} ${String(hour).padStart(2, "0")}:00 — ${value}`,
            })),
        }));
    }

    get busiest() {
        const { days, grid } = this.props.data;
        let best = { value: 0, day: "", hour: 0 };
        days.forEach((day, index) => {
            grid[index].forEach((value, hour) => {
                if (value > best.value) {
                    best = { value, day, hour };
                }
            });
        });
        // Formatted here, not in the template: OWL evaluates template
        // expressions against the component, so JS globals like String()
        // are not in scope there.
        return { ...best, hourLabel: String(best.hour).padStart(2, "0") };
    }

    get isEmpty() {
        return !this.props.data.total;
    }
}
