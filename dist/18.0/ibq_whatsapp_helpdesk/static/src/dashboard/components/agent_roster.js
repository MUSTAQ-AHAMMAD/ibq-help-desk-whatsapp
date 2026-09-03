/** @odoo-module **/

import { Component, useState } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";

const STATUSES = [
    { key: "available", label: _t("Available") },
    { key: "busy", label: _t("Busy") },
    { key: "away", label: _t("Away") },
    { key: "offline", label: _t("Offline") },
];

const ROLES = [
    { key: "owner", label: _t("Owner"),
      hint: _t("Everything, including transferring ownership.") },
    { key: "admin", label: _t("Administrator"),
      hint: _t("The roster, roles, settings, and every chat.") },
    { key: "supervisor", label: _t("Supervisor"),
      hint: _t("Every chat in their departments, saved replies, tags, exports.") },
    { key: "agent", label: _t("Agent"),
      hint: _t("Their own chats and their own presence.") },
];

/**
 * The Team tab: who answers WhatsApp, what they may do, and how loaded
 * they are.
 *
 * Every control here is mirrored by a server-side check. The UI hides what
 * you cannot do so the interface stays honest, not so the rule is enforced.
 */
export class AgentRoster extends Component {
    static template = "ibq_whatsapp_helpdesk.AgentRoster";
    static props = {
        agents: { type: Array },
        departments: { type: Array },
        onAgentsChanged: { type: Function },
        rights: { type: Array },
        myRole: { type: String },
    };

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.notification = useService("notification");
        this.statuses = STATUSES;
        this.roles = ROLES;

        this.state = useState({
            adding: false,
            search: "",
            candidates: [],
            selected: [],
            newRole: "agent",
            departmentIds: [],
            busy: false,
            expanded: null,
        });
    }

    has(right) {
        return this.props.rights.includes(right);
    }

    /** Roles this person is allowed to hand out. */
    get grantableRoles() {
        if (this.props.myRole === "owner") {
            return this.roles.filter((r) => r.key !== "owner");
        }
        return this.roles.filter((r) => ["supervisor", "agent"].includes(r.key));
    }

    // ------------------------------------------------------------------
    // Add flow
    // ------------------------------------------------------------------
    async openAdd() {
        this.state.adding = true;
        this.state.selected = [];
        this.state.search = "";
        this.state.newRole = "agent";
        this.state.departmentIds = [];
        this.state.candidates = await this.orm.call(
            "whatsapp.dashboard", "get_candidate_users", []
        );
    }

    closeAdd() {
        this.state.adding = false;
    }

    async onSearch(ev) {
        this.state.search = ev.target.value;
        this.state.candidates = await this.orm.call(
            "whatsapp.dashboard", "get_candidate_users", [this.state.search]
        );
    }

    toggleCandidate(userId) {
        const index = this.state.selected.indexOf(userId);
        if (index === -1) {
            this.state.selected.push(userId);
        } else {
            this.state.selected.splice(index, 1);
        }
    }

    isSelected(userId) {
        return this.state.selected.includes(userId);
    }

    toggleDepartment(teamId) {
        const index = this.state.departmentIds.indexOf(teamId);
        if (index === -1) {
            this.state.departmentIds.push(teamId);
        } else {
            this.state.departmentIds.splice(index, 1);
        }
    }

    async confirmAdd() {
        if (!this.state.selected.length) {
            return;
        }
        this.state.busy = true;
        try {
            const agents = await this.orm.call("whatsapp.dashboard", "add_agents", [
                this.state.selected, this.state.departmentIds,
            ], { role: this.state.newRole });
            this.props.onAgentsChanged(agents);
            this.notification.add(
                _t("%s person(s) added to the WhatsApp team.", this.state.selected.length),
                { type: "success" }
            );
            this.state.adding = false;
        } catch (error) {
            this.notify(error);
        } finally {
            this.state.busy = false;
        }
    }

    openInviteWizard() {
        this.action.doAction("ibq_whatsapp_helpdesk.action_whatsapp_invite_member");
    }

    // ------------------------------------------------------------------
    // Row actions
    // ------------------------------------------------------------------
    toggleExpanded(agent) {
        this.state.expanded = this.state.expanded === agent.id ? null : agent.id;
    }

    async setRole(agent, ev) {
        const role = ev.target.value;
        if (role === agent.role) {
            return;
        }
        await this.update(agent, { role });
    }

    async setStatus(agent, ev) {
        await this.update(agent, { status: ev.target.value });
    }

    async setCapacity(agent, ev) {
        const value = parseInt(ev.target.value, 10);
        if (!value || value < 1) {
            ev.target.value = agent.capacity;
            return;
        }
        await this.update(agent, { max_active_chats: value });
    }

    async toggleAutoAssign(agent) {
        await this.update(agent, { auto_assign: !agent.auto_assign });
    }

    async toggleAgentDepartment(agent, teamId) {
        const current = [...agent.department_ids];
        const index = current.indexOf(teamId);
        if (index === -1) {
            current.push(teamId);
        } else {
            current.splice(index, 1);
        }
        await this.update(agent, { team_ids: [[6, 0, current]] });
    }

    async update(agent, values) {
        try {
            const agents = await this.orm.call("whatsapp.dashboard", "update_agent", [
                agent.id, values,
            ]);
            this.props.onAgentsChanged(agents);
        } catch (error) {
            this.notify(error);
        }
    }

    async remove(agent) {
        try {
            const agents = await this.orm.call("whatsapp.dashboard", "remove_agent", [agent.id]);
            this.props.onAgentsChanged(agents);
            this.notification.add(_t("%s removed from the team.", agent.name), {
                type: "info",
            });
        } catch (error) {
            this.notify(error);
        }
    }

    notify(error) {
        this.notification.add(
            error.message?.data?.message || error.message || _t("Something went wrong."),
            { type: "danger" }
        );
    }

    // ------------------------------------------------------------------
    // Presentation
    // ------------------------------------------------------------------
    get sorted() {
        const roleRank = { owner: 0, admin: 1, supervisor: 2, agent: 3 };
        const statusRank = { available: 0, busy: 1, away: 2, offline: 3 };
        return [...this.props.agents].sort(
            (a, b) =>
                roleRank[a.role] - roleRank[b.role] ||
                statusRank[a.status] - statusRank[b.status] ||
                a.name.localeCompare(b.name)
        );
    }

    get roleCounts() {
        const counts = {};
        for (const agent of this.props.agents) {
            counts[agent.role] = (counts[agent.role] || 0) + 1;
        }
        return this.roles.map((role) => ({ ...role, count: counts[role.key] || 0 }));
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

    canEditRow(agent) {
        return this.has("manage_roster") && (agent.can_edit || agent.role !== "owner");
    }
}
