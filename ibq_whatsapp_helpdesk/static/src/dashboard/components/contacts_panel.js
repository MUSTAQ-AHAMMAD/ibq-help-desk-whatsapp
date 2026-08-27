/** @odoo-module **/

import { Component, onWillStart, useState } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";

/**
 * Everyone who has ever written in, and the blocklist.
 *
 * Grouped by number rather than by contact: an unknown number has no partner
 * record yet but is still someone the team has talked to.
 */
export class ContactsPanel extends Component {
    static template = "ibq_whatsapp_helpdesk.ContactsPanel";
    static props = {
        onOpenConversation: { type: Function },
        canBlock: { type: Boolean },
    };

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.notification = useService("notification");

        this.reasons = [
            { key: "spam", label: _t("Spam") },
            { key: "abuse", label: _t("Abusive") },
            { key: "test", label: _t("Test number") },
            { key: "other", label: _t("Other") },
        ];

        this.state = useState({
            view: "contacts",
            loading: true,
            search: "",
            contacts: [],
            total: 0,
            blocklist: [],
            blocking: null,
            blockReason: "spam",
            blockNote: "",
        });

        onWillStart(() => this.load());
    }

    async load() {
        this.state.loading = true;
        try {
            const result = await this.orm.call("whatsapp.dashboard", "get_contacts", [], {
                search: this.state.search || null,
            });
            this.state.contacts = result.contacts;
            this.state.total = result.total;
            if (this.props.canBlock) {
                this.state.blocklist = await this.orm.call(
                    "whatsapp.dashboard", "get_blocklist", []
                );
            }
        } finally {
            this.state.loading = false;
        }
    }

    async onSearch(ev) {
        this.state.search = ev.target.value;
        await this.load();
    }

    setView(view) {
        this.state.view = view;
    }

    openPartner(contact) {
        if (!contact.partner_id) {
            return;
        }
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "res.partner",
            res_id: contact.partner_id,
            views: [[false, "form"]],
        });
    }

    // ------------------------------------------------------------------
    // Blocking
    // ------------------------------------------------------------------
    startBlock(contact) {
        this.state.blocking = contact;
        this.state.blockReason = "spam";
        this.state.blockNote = "";
    }

    cancelBlock() {
        this.state.blocking = null;
    }

    async confirmBlock() {
        const contact = this.state.blocking;
        try {
            this.state.blocklist = await this.orm.call(
                "whatsapp.dashboard", "block_number", [contact.number], {
                    reason: this.state.blockReason,
                    note: this.state.blockNote || null,
                }
            );
            this.state.blocking = null;
            await this.load();
            this.notification.add(
                _t("%s is blocked. Their messages are now dropped on arrival.",
                   contact.number),
                { type: "success" }
            );
        } catch (error) {
            this.notification.add(
                error.message?.data?.message || _t("Could not block that number."),
                { type: "warning" }
            );
        }
    }

    async unblock(entry) {
        try {
            this.state.blocklist = await this.orm.call(
                "whatsapp.dashboard", "unblock_number", [entry.id]
            );
            await this.load();
        } catch (error) {
            this.notification.add(
                error.message?.data?.message || _t("Could not unblock that number."),
                { type: "warning" }
            );
        }
    }

    // ------------------------------------------------------------------
    // Presentation
    // ------------------------------------------------------------------
    formatDate(value) {
        if (!value) {
            return "";
        }
        const date = new Date(value.replace(" ", "T") + "Z");
        return date.toLocaleDateString(undefined, {
            day: "2-digit", month: "short", year: "numeric",
        });
    }

    stars(score) {
        return score ? "★".repeat(score) + "☆".repeat(5 - score) : "";
    }
}
