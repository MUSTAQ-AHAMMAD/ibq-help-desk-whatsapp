/** @odoo-module **/

import { Component, onWillStart, onWillUpdateProps, useState } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";

import { Heatmap } from "./heatmap";

/**
 * The reporting tab: agent leaderboard, when-are-we-busy heatmap, what chats
 * are about, how long customers wait, and how happy they were.
 *
 * Every table has a CSV export behind the same right the server checks.
 */
export class ReportsPanel extends Component {
    static template = "ibq_whatsapp_helpdesk.ReportsPanel";
    static components = { Heatmap };
    static props = {
        filters: { type: Object },
        canExport: { type: Boolean },
        formatDuration: { type: Function },
    };

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.notification = useService("notification");
        this.state = useState({ loading: true, reports: null, exporting: false });

        onWillStart(() => this.load());

        // Reload whenever the shared filters change. Comparing the serialised
        // filters is enough for an object of five scalar-ish keys, and avoids
        // pulling in a deep-watch helper.
        onWillUpdateProps((nextProps) => {
            if (JSON.stringify(nextProps.filters) !== JSON.stringify(this.props.filters)) {
                return this.load(nextProps.filters);
            }
        });
    }

    async load(filters) {
        this.state.loading = true;
        try {
            this.state.reports = await this.orm.call(
                "whatsapp.dashboard", "get_reports", [], {
                    filters: { ...(filters || this.props.filters) },
                }
            );
        } finally {
            this.state.loading = false;
        }
    }

    async exportCsv(kind) {
        this.state.exporting = true;
        try {
            const result = await this.orm.call("whatsapp.dashboard", "export_report", [kind], {
                filters: { ...this.props.filters },
            });
            this.action.doAction({
                type: "ir.actions.act_url",
                url: result.url,
                target: "self",
            });
            this.notification.add(
                _t("%(rows)s row(s) exported to %(name)s.", {
                    rows: result.rows, name: result.name,
                }),
                { type: "success" }
            );
        } catch (error) {
            this.notification.add(
                error.message?.data?.message || _t("Export failed."),
                { type: "warning" }
            );
        } finally {
            this.state.exporting = false;
        }
    }

    // ------------------------------------------------------------------
    // Presentation
    // ------------------------------------------------------------------
    get leaderboard() {
        return this.state.reports?.leaderboard || [];
    }

    get csat() {
        return this.state.reports?.csat || { distribution: [], total: 0, average: 0 };
    }

    get tags() {
        return this.state.reports?.tags || { rows: [], untagged: 0 };
    }

    get tagMax() {
        return Math.max(1, ...this.tags.rows.map((r) => r.value));
    }

    get departments() {
        return this.state.reports?.departments || [];
    }

    get departmentMax() {
        return Math.max(1, ...this.departments.map((r) => r.value));
    }

    get issues() {
        return this.state.reports?.issues || {
            rows: [], repeated: [], unique_count: 0, repeated_count: 0,
            distinct_count: 0, classified: 0, unclassified: 0,
            repeat_share: 0, contacts: 0, repeat_contacts: 0,
        };
    }

    get issueMax() {
        return Math.max(1, ...this.issues.rows.map((r) => r.count));
    }

    openIssue(issue) {
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "whatsapp.issue",
            res_id: issue.id,
            views: [[false, "form"]],
        });
    }

    get buckets() {
        return this.state.reports?.response_buckets || { rows: [], total: 0 };
    }

    csatClass(score) {
        if (score >= 4) {
            return "o_wa_csat--happy";
        }
        return score === 3 ? "o_wa_csat--neutral" : "o_wa_csat--unhappy";
    }

    /** Odoo's tag palette, so a tag looks the same here as in the list views. */
    tagColor(index) {
        const palette = [
            "#8f9fa8", "#c9503a", "#d69433", "#3a8fc9", "#7a4fb5",
            "#c95a8f", "#2fa36b", "#5a7fc9", "#b56b2f", "#4aa3a3",
            "#a33b6b", "#6b8f2f",
        ];
        return palette[index % palette.length];
    }
}
