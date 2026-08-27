/** @odoo-module **/

import {
    Component, onWillStart, onWillUnmount, useEffect, useRef, useState,
} from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";

const QUEUE_POLL_MS = 45000;

/**
 * The agent console: queue on the left, thread on the right.
 *
 * Reacts to the server's bus nudge so a new customer message lands without a
 * refresh, and falls back to a slow poll in case the websocket is down.
 */
export class ChatConsole extends Component {
    static template = "ibq_whatsapp_helpdesk.ChatConsole";
    static props = {
        agents: { type: Array },
        departments: { type: Array },
        tags: { type: Array },
        filters: { type: Object },
        rights: { type: Array },
        openId: { type: [Number, { value: false }], optional: true },
    };

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.notification = useService("notification");
        // Reached through env.services, not useService: bus_service declares
        // `async: true`, which useService only knows how to wrap for a
        // function-shaped service. Core code does the same.
        this.busService = this.env.services.bus_service;

        this.threadRef = useRef("thread");
        this.composerRef = useRef("composer");

        this.scopes = [
            { key: "waiting", label: _t("Waiting") },
            { key: "mine", label: _t("Mine") },
            { key: "unassigned", label: _t("Unassigned") },
            { key: "bot", label: _t("With bot") },
            { key: "all", label: _t("Open") },
            { key: "closed", label: _t("Closed") },
        ];
        this.priorities = [
            { key: "0", label: _t("Low") },
            { key: "1", label: _t("Normal") },
            { key: "2", label: _t("High") },
            { key: "3", label: _t("Urgent") },
        ];

        this.state = useState({
            scope: "waiting",
            search: "",
            conversations: [],
            counts: {},
            active: null,
            draft: "",
            note: "",
            panel: "context",       // context | notes | canned
            canned: [],
            cannedSearch: "",
            showCanned: false,
            showTags: false,
            showTransfer: false,
            transferUser: "",
            transferTeam: "",
            loadingQueue: true,
            loadingThread: false,
            sending: false,
        });

        onWillStart(async () => {
            await this.loadQueue();
            if (this.props.openId) {
                await this.openConversation(this.props.openId);
            }
            this.busService.subscribe("ibq_whatsapp/dashboard", (payload) =>
                this.onServerEvent(payload)
            );
        });

        this.timer = setInterval(() => this.loadQueue(true), QUEUE_POLL_MS);
        onWillUnmount(() => clearInterval(this.timer));

        // Keep the thread pinned to the newest message as it grows.
        useEffect(
            () => this.scrollToBottom(),
            () => [this.state.active?.id, this.state.active?.messages.length]
        );
    }

    has(right) {
        return this.props.rights.includes(right);
    }

    // ------------------------------------------------------------------
    // Data
    // ------------------------------------------------------------------
    async loadQueue(silent = false) {
        if (!silent) {
            this.state.loadingQueue = true;
        }
        try {
            const result = await this.orm.call("whatsapp.dashboard", "get_conversations", [], {
                scope: this.state.scope,
                search: this.state.search || null,
                filters: { ...this.props.filters },
            });
            this.state.conversations = result.conversations;
            this.state.counts = result.counts;
        } finally {
            this.state.loadingQueue = false;
        }
    }

    async openConversation(conversationId) {
        this.state.loadingThread = true;
        try {
            this.state.active = await this.orm.call(
                "whatsapp.dashboard", "get_conversation", [conversationId]
            );
            this.state.draft = "";
            this.state.note = "";
            this.state.showCanned = false;
        } catch (error) {
            this.notify(error);
        } finally {
            this.state.loadingThread = false;
        }
    }

    async refreshThread() {
        if (this.state.active) {
            this.state.active = await this.orm.call(
                "whatsapp.dashboard", "get_conversation", [this.state.active.id]
            );
        }
    }

    onServerEvent(payload) {
        this.loadQueue(true);
        if (this.state.active && payload.conversation_id === this.state.active.id) {
            this.refreshThread();
        }
    }

    async setScope(scope) {
        this.state.scope = scope;
        await this.loadQueue();
    }

    async onSearch(ev) {
        this.state.search = ev.target.value;
        await this.loadQueue(true);
    }

    // ------------------------------------------------------------------
    // Composing
    // ------------------------------------------------------------------
    onComposerKeydown(ev) {
        // Enter sends, Shift+Enter breaks the line: the convention every
        // messaging app already trained these agents on.
        if (ev.key === "Enter" && !ev.shiftKey) {
            ev.preventDefault();
            this.send();
            return;
        }
        if (ev.key === "Escape" && this.state.showCanned) {
            this.state.showCanned = false;
        }
    }

    /**
     * Typing "/" at the start of an empty-ish draft opens the saved replies,
     * and keeps filtering as the shortcut is typed.
     */
    async onComposerInput(ev) {
        this.state.draft = ev.target.value;
        const match = this.state.draft.match(/^\/([a-z0-9_-]*)$/i);
        if (match) {
            this.state.cannedSearch = match[1];
            this.state.showCanned = true;
            await this.loadCanned();
        } else if (this.state.showCanned) {
            this.state.showCanned = false;
        }
    }

    async loadCanned() {
        this.state.canned = await this.orm.call(
            "whatsapp.dashboard", "get_canned_responses", [], {
                search: this.state.cannedSearch || null,
                conversation_id: this.state.active?.id || null,
            }
        );
    }

    async toggleCanned() {
        this.state.showCanned = !this.state.showCanned;
        if (this.state.showCanned) {
            this.state.cannedSearch = "";
            await this.loadCanned();
        }
    }

    insertCanned(canned) {
        this.state.draft = canned.body;
        this.state.showCanned = false;
        this.lastCannedId = canned.id;
        this.composerRef.el?.focus();
    }

    async send() {
        const body = this.state.draft.trim();
        if (!body || this.state.sending || !this.state.active) {
            return;
        }
        this.state.sending = true;
        try {
            this.state.active = await this.orm.call("whatsapp.dashboard", "send_message", [
                this.state.active.id, body,
            ], { canned_id: this.lastCannedId || null });
            this.state.draft = "";
            this.lastCannedId = null;
            await this.loadQueue(true);
        } catch (error) {
            this.notify(error);
        } finally {
            this.state.sending = false;
            this.composerRef.el?.focus();
        }
    }

    // ------------------------------------------------------------------
    // Notes
    // ------------------------------------------------------------------
    async addNote() {
        const body = this.state.note.trim();
        if (!body) {
            return;
        }
        try {
            this.state.active = await this.orm.call("whatsapp.dashboard", "add_note", [
                this.state.active.id, body,
            ]);
            this.state.note = "";
        } catch (error) {
            this.notify(error);
        }
    }

    // ------------------------------------------------------------------
    // Triage
    // ------------------------------------------------------------------
    async toggleTag(tag) {
        const current = this.state.active.tags.map((t) => t.id);
        const index = current.indexOf(tag.id);
        if (index === -1) {
            current.push(tag.id);
        } else {
            current.splice(index, 1);
        }
        try {
            this.state.active = await this.orm.call(
                "whatsapp.dashboard", "set_conversation_tags",
                [this.state.active.id, current]
            );
            await this.loadQueue(true);
        } catch (error) {
            this.notify(error);
        }
    }

    hasTag(tag) {
        return (this.state.active?.tags || []).some((t) => t.id === tag.id);
    }

    async setPriority(ev) {
        try {
            await this.orm.call("whatsapp.dashboard", "set_conversation_priority",
                                [this.state.active.id, ev.target.value]);
            await Promise.all([this.refreshThread(), this.loadQueue(true)]);
        } catch (error) {
            this.notify(error);
        }
    }

    // ------------------------------------------------------------------
    // Conversation actions
    // ------------------------------------------------------------------
    async act(action) {
        if (!this.state.active) {
            return;
        }
        try {
            await this.orm.call("whatsapp.dashboard", "act_on_conversation",
                                [this.state.active.id, action]);
            await Promise.all([this.refreshThread(), this.loadQueue(true)]);
        } catch (error) {
            this.notify(error);
        }
    }

    async assign(ev) {
        const userId = parseInt(ev.target.value, 10) || false;
        try {
            await this.orm.call("whatsapp.dashboard", "assign_conversation",
                                [this.state.active.id, userId]);
            await Promise.all([this.refreshThread(), this.loadQueue(true)]);
        } catch (error) {
            this.notify(error);
        }
    }

    toggleTransfer() {
        this.state.showTransfer = !this.state.showTransfer;
        this.state.transferUser = "";
        this.state.transferTeam = "";
    }

    async confirmTransfer() {
        try {
            this.state.active = await this.orm.call(
                "whatsapp.dashboard", "transfer_conversation",
                [this.state.active.id], {
                    user_id: parseInt(this.state.transferUser, 10) || null,
                    team_id: parseInt(this.state.transferTeam, 10) || null,
                }
            );
            this.state.showTransfer = false;
            await this.loadQueue(true);
        } catch (error) {
            this.notify(error);
        }
    }

    openTicket() {
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "helpdesk.ticket",
            res_id: this.state.active.ticket_id,
            views: [[false, "form"]],
        });
    }

    // ------------------------------------------------------------------
    // Presentation
    // ------------------------------------------------------------------
    scrollToBottom() {
        const el = this.threadRef.el;
        if (el) {
            el.scrollTop = el.scrollHeight;
        }
    }

    notify(error) {
        this.notification.add(
            error.message?.data?.message || error.message || _t("Something went wrong."),
            { type: "warning" }
        );
    }

    get canSend() {
        const active = this.state.active;
        return Boolean(active && active.state !== "closed" && active.in_session);
    }

    get composerHint() {
        const active = this.state.active;
        if (!active) {
            return "";
        }
        if (active.state === "closed") {
            return _t("This conversation is closed. Reopen it to reply.");
        }
        if (!active.in_session) {
            return _t(
                "The 24h WhatsApp session has expired. Only an approved template " +
                "can be delivered — send one from the ticket."
            );
        }
        return "";
    }

    get answerRows() {
        const answers = this.state.active?.answers || {};
        return Object.keys(answers).map((key) => ({
            key,
            label: key.replace(/_/g, " "),
            value: answers[key],
        }));
    }

    formatTime(value) {
        if (!value) {
            return "";
        }
        // Server datetimes arrive as naive UTC strings.
        const date = new Date(value.replace(" ", "T") + "Z");
        return date.toLocaleString(undefined, {
            hour: "2-digit", minute: "2-digit", day: "2-digit", month: "short",
        });
    }

    relative(value) {
        if (!value) {
            return "";
        }
        const then = new Date(value.replace(" ", "T") + "Z");
        const seconds = Math.max(0, (Date.now() - then.getTime()) / 1000);
        if (seconds < 60) {
            return _t("just now");
        }
        if (seconds < 3600) {
            return _t("%sm", Math.floor(seconds / 60));
        }
        if (seconds < 86400) {
            return _t("%sh", Math.floor(seconds / 3600));
        }
        return _t("%sd", Math.floor(seconds / 86400));
    }

    stateLabel(state) {
        return { bot: _t("Bot"), agent: _t("Agent"), closed: _t("Closed") }[state] || state;
    }

    priorityLabel(priority) {
        return (this.priorities.find((p) => p.key === priority) || {}).label || "";
    }

    stars(score) {
        return score ? "★".repeat(score) + "☆".repeat(5 - score) : "";
    }
}
