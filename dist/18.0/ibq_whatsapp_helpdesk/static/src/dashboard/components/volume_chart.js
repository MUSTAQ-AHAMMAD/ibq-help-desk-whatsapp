/** @odoo-module **/

import { Component } from "@odoo/owl";

const VIEW_W = 760;
const VIEW_H = 200;
const PAD = { top: 12, right: 8, bottom: 24, left: 34 };

/**
 * Daily inbound vs outbound volume, drawn as paired bars.
 *
 * Hand-rolled SVG rather than a charting library: the shape is simple, and
 * an inline SVG inherits the dashboard's own theme tokens through
 * currentColor instead of needing a second palette.
 */
export class VolumeChart extends Component {
    static template = "ibq_whatsapp_helpdesk.VolumeChart";
    static props = {
        points: { type: Array },
    };

    get viewBox() {
        return `0 0 ${VIEW_W} ${VIEW_H}`;
    }

    get plot() {
        return {
            w: VIEW_W - PAD.left - PAD.right,
            h: VIEW_H - PAD.top - PAD.bottom,
            x: PAD.left,
            y: PAD.top,
        };
    }

    get max() {
        const values = this.props.points.flatMap((p) => [p.inbound, p.outbound]);
        return Math.max(1, ...values);
    }

    /** Round the axis up to a readable ceiling (5, 10, 25, 50, 100...). */
    get ceiling() {
        const max = this.max;
        const magnitude = Math.pow(10, Math.floor(Math.log10(max)));
        for (const step of [1, 2, 2.5, 5, 10]) {
            const candidate = step * magnitude;
            if (candidate >= max) {
                return candidate;
            }
        }
        return magnitude * 10;
    }

    get gridLines() {
        const { h, y, w, x } = this.plot;
        return [0, 0.25, 0.5, 0.75, 1].map((ratio) => ({
            y: y + h - h * ratio,
            x1: x,
            x2: x + w,
            label: Math.round(this.ceiling * ratio),
            major: ratio === 0,
        }));
    }

    get bars() {
        const { w, h, x, y } = this.plot;
        const points = this.props.points;
        const slot = w / Math.max(points.length, 1);
        const barW = Math.max(2, Math.min(11, (slot - 4) / 2));
        const ceiling = this.ceiling;

        const scale = (value) => (value / ceiling) * h;

        return points.map((point, index) => {
            const centre = x + slot * index + slot / 2;
            const inH = scale(point.inbound);
            const outH = scale(point.outbound);
            return {
                key: point.date,
                label: point.label,
                short: point.short,
                showLabel: points.length <= 10 || index % Math.ceil(points.length / 8) === 0,
                labelX: centre,
                inbound: {
                    x: centre - barW - 1,
                    y: y + h - inH,
                    width: barW,
                    height: Math.max(inH, point.inbound ? 1.5 : 0),
                    value: point.inbound,
                },
                outbound: {
                    x: centre + 1,
                    y: y + h - outH,
                    width: barW,
                    height: Math.max(outH, point.outbound ? 1.5 : 0),
                    value: point.outbound,
                },
            };
        });
    }

    get axisY() {
        return this.plot.y + this.plot.h + 16;
    }

    get isEmpty() {
        return !this.props.points.length ||
            this.props.points.every((p) => !p.inbound && !p.outbound);
    }
}
