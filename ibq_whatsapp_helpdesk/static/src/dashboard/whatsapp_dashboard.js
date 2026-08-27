/** @odoo-module **/

import { Component, onWillStart, onWillUnmount, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";

import { StatTile } from "./components/stat_tile";
import { VolumeChart } from "./components/volume_chart";
import { MixBar } from "./components/mix_bar";
import { FilterBar } from "./components/filter_bar";
import { MonitorBoard } from "./components/monitor_board";
import { ChatConsole } from "./components/chat_console";
import { ReportsPanel } from "./components/reports_panel";
import { ContactsPanel } from "./components/contacts_panel";
import { AgentRoster } from "./components/agent_roster";

const REFRESH_MS = 60000;

/**
 * The WhatsApp dashboard.
 *
 * Six tabs over one client action. What each person sees is decided by their
 * WhatsApp role: the tab strip, the filter bar and every button check the
 * rights the server handed back at bootstrap, and the server re-checks them
 * on every call that changes something.
 */
export class WhatsappDashboard extends Component {
    static template = "ibq_whatsapp_helpdesk.WhatsappDashboard";
    static components = {
        StatTile, VolumeChart, MixBar, FilterBar,
        MonitorBoard, ChatConsole, ReportsPanel, ContactsPanel, AgentRoster,
    };
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.notification = useService("notification");
        // Reached through env.services, not useService: bus_service declares
        // `async: true`, which useService only knows how to wrap for a
        // function-shaped service. Core code does the same.
        this.busService = this.env.services.bus_service;

        this.state = useState({
            ready: false,
            loading: true,
            tab: "overview",
            openId: false,
            boot: null,
            data: null,
            error: null,
            filters: {
                period: "7d",
                team_id: false,
                user_id: false,
                account_id: false,
                tag_ids: [],
            },
        });

        this.tabs = [
            { key: "overview", label: _t("Overview"), right: null },
            { key: "monitor", label: _t("Monitoring"), right: null },
            { key: "inbox", label: _t("Inbox"), right: null },
            { key: "reports", label: _t("Reports"), right: null },
            { key: "contacts", label: _t("Contacts"), right: null },
            { key: "team", label: _t("Team"), right: null },
        ];

        this.statuses = [
            { key: "available", label: _t("Available") },
            { key: "busy", label: _t("Busy") },
            { key: "away", label: _t("Away") },
            { key: "offline", label: _t("Offline") },
        ];

        onWillStart(async () => {
            this.state.boot = await this.orm.call("whatsapp.dashboard", "get_bootstrap", []);
            await this.loadStats();
            this.state.ready = true;
            this.busService.subscribe("ibq_whatsapp/dashboard", (payload) =>
                this.onServerEvent(payload)
            );
        });

        this.timer = setInterval(() => this.loadStats(true), REFRESH_MS);
        onWillUnmount(() => clearInterval(this.timer));
    }

    // ------------------------------------------------------------------
    // Rights
    // ------------------------------------------------------------------
    has(right) {
        return (this.state.boot?.rights || []).includes(right);
    }

    get me() {
        return this.state.boot?.me || {};
    }

    get kpi() {
        return this.state.data?.kpi || {};
    }

    get live() {
        return this.state.data?.live || {};
    }

    // ------------------------------------------------------------------
    // Data
    // ------------------------------------------------------------------
    async loadStats(silent = false) {
        if (!silent) {
            this.state.loading = true;
        }
        try {
            this.state.data = await this.orm.call(
                "whatsapp.dashboard", "get_dashboard_data", [], {
                    filters: { ...this.state.filters },
                }
            );
            this.state.error = null;
        } catch (error) {
            // A background refresh must never throw a dialog over someone's work.
            this.state.error =
                error.message?.data?.message || error.message || String(error);
            if (!silent) {
                throw error;
            }
        } finally {
            this.state.loading = false;
        }
    }

    async reloadBoot() {
        this.state.boot = await this.orm.call("whatsapp.dashboard", "get_bootstrap", []);
    }

    onServerEvent(payload) {
        // The Inbox and Monitoring tabs react on their own; here we only keep
        // the headline counters honest between timer ticks.
        if (["handoff", "closed", "assigned", "message", "rated"].includes(payload.event)) {
            this.loadStats(true);
        }
    }

    // ------------------------------------------------------------------
    // Interaction
    // ------------------------------------------------------------------
    setTab(tab) {
        this.state.tab = tab;
        if (tab !== "inbox") {
            this.state.openId = false;
        }
    }

    /** Jump to the Inbox with one chat already open. */
    openConversation(conversationId) {
        this.state.openId = conversationId;
        this.state.tab = "inbox";
    }

    async onFiltersChanged(filters) {
        Object.assign(this.state.filters, filters);
        await this.loadStats();
    }

    async setMyStatus(status) {
        try {
            const me = await this.orm.call("whatsapp.dashboard", "set_my_status", [status]);
            this.state.boot.me = me;
            await this.loadStats(true);
        } catch (error) {
            this.notify(error, _t("Could not change your status."));
        }
    }

    onAgentsChanged(agents) {
        this.state.boot.agents = agents;
        const me = agents.find((a) => a.is_me);
        if (me) {
            this.state.boot.me = me;
        }
    }

    notify(error, fallback) {
        this.notification.add(
            error?.message?.data?.message || error?.message || fallback ||
                _t("Something went wrong."),
            { type: "warning" }
        );
    }

    // ------------------------------------------------------------------
    // Formatting
    // ------------------------------------------------------------------
    /** Seconds to a compact "4m 12s" / "2h 05m" reading. */
    formatDuration(seconds) {
        if (!seconds) {
            return "—";
        }
        if (seconds < 60) {
            return `${Math.round(seconds)}s`;
        }
        if (seconds < 3600) {
            const m = Math.floor(seconds / 60);
            return `${m}m ${String(Math.round(seconds % 60)).padStart(2, "0")}s`;
        }
        const h = Math.floor(seconds / 3600);
        const m = Math.floor((seconds % 3600) / 60);
        return `${h}h ${String(m).padStart(2, "0")}m`;
    }
}

registry.category("actions").add("ibq_whatsapp_dashboard", WhatsappDashboard);
