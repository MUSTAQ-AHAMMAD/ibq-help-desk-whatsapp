/** @odoo-module **/

import { Component, useState } from "@odoo/owl";

/**
 * The shared filter bar: period, department, agent, sender, tags.
 *
 * One source of truth for every tab that reports numbers, so switching from
 * Overview to Reports keeps the same slice of data rather than silently
 * resetting it.
 */
export class FilterBar extends Component {
    static template = "ibq_whatsapp_helpdesk.FilterBar";
    static props = {
        filters: { type: Object },
        periods: { type: Array },
        departments: { type: Array },
        agents: { type: Array },
        accounts: { type: Array },
        tags: { type: Array },
        onChange: { type: Function },
        compact: { type: Boolean, optional: true },
    };

    setup() {
        this.state = useState({ tagsOpen: false });
    }

    setPeriod(period) {
        this.props.onChange({ period });
    }

    onSelect(key, ev) {
        const raw = ev.target.value;
        this.props.onChange({ [key]: raw ? parseInt(raw, 10) : false });
    }

    toggleTag(tagId) {
        const current = [...(this.props.filters.tag_ids || [])];
        const index = current.indexOf(tagId);
        if (index === -1) {
            current.push(tagId);
        } else {
            current.splice(index, 1);
        }
        this.props.onChange({ tag_ids: current });
    }

    isTagOn(tagId) {
        return (this.props.filters.tag_ids || []).includes(tagId);
    }

    clear() {
        this.props.onChange({
            team_id: false,
            user_id: false,
            account_id: false,
            tag_ids: [],
        });
    }

    get activeCount() {
        const f = this.props.filters;
        return [f.team_id, f.user_id, f.account_id].filter(Boolean).length +
            (f.tag_ids || []).length;
    }
}
