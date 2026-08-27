/** @odoo-module **/

import { Component, onWillStart, onWillUnmount, useState } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";

const POLL_MS = 20000;

/**
 * The wallboard: three columns of live chats and who is on shift.
 *
 * Refreshes faster than the rest of the dashboard because it is the tab a
 * supervisor leaves on a second screen.
 */
export class MonitorBoard extends Component {
    static template = "ibq_whatsapp_helpdesk.MonitorBoard";
    static props = {
        onOpenConversation: { type: Function },
        canReassign: { type: Boolean },
    };

    setup() {
        this.orm = useService("orm");
        this.notification = useService("notification");
        // Reached through env.services, not useService: bus_service declares
        // `async: true`, which useService only knows how to wrap for a
        // function-shaped service. Core code does the same.
        this.busService = this.env.services.bus_service;
        this.state = useState({ loading: true, columns: [], agents: [], stamp: "" });

        onWillStart(async () => {
            await this.load();
            this.busService.subscribe("ibq_whatsapp/dashboard", () => this.load(true));
        });
        this.timer = setInterval(() => this.load(true), POLL_MS);
        onWillUnmount(() => clearInterval(this.timer));
    }

    async load(silent = false) {
        if (!silent) {
            this.state.loading = true;
        }
        try {
            const data = await this.orm.call("whatsapp.dashboard", "get_monitoring", []);
            this.state.columns = data.columns;
            this.state.agents = data.agents;
            this.state.stamp = data.generated_at;
        } finally {
            this.state.loading = false;
        }
    }

    async claim(conversation) {
        try {
            await this.orm.call("whatsapp.dashboard", "act_on_conversation", [
                conversation.id, "take_over",
            ]);
            await this.load(true);
            this.props.onOpenConversation(conversation.id);
        } catch (error) {
            this.notification.add(
                error.message?.data?.message || _t("Could not claim that chat."),
                { type: "warning" }
            );
        }
    }

    /** Minutes a chat has been sitting, used to colour the waiting column. */
    ageMinutes(conversation) {
        if (!conversation.last_message_date) {
            return 0;
        }
        const then = new Date(conversation.last_message_date.replace(" ", "T") + "Z");
        return Math.floor((Date.now() - then.getTime()) / 60000);
    }

    ageClass(conversation) {
        const minutes = this.ageMinutes(conversation);
        if (minutes >= 30) {
            return "o_wa_age--critical";
        }
        if (minutes >= 10) {
            return "o_wa_age--warn";
        }
        return "o_wa_age--ok";
    }

    ageLabel(conversation) {
        const minutes = this.ageMinutes(conversation);
        if (minutes < 1) {
            return _t("just now");
        }
        if (minutes < 60) {
            return _t("%sm", minutes);
        }
        return _t("%sh", Math.floor(minutes / 60));
    }

    get onShift() {
        const rank = { available: 0, busy: 1, away: 2, offline: 3 };
        return [...this.state.agents].sort(
            (a, b) => rank[a.status] - rank[b.status] || b.active_chats - a.active_chats
        );
    }

    loadClass(agent) {
        if (agent.load_percent >= 100) {
            return "o_wa_load--full";
        }
        if (agent.load_percent >= 70) {
            return "o_wa_load--high";
        }
        return "o_wa_load--ok";
    }
}
