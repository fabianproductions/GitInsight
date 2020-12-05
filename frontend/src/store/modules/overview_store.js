import axios from "axios";

import { API_BASE_PATH } from '@/store/constants'
import DataFrame from "dataframe-js";

export const overview_store = {
    state: {
        last_days: 365,

        count_and_team_of_dirs_data: null,
        count_and_team_is_loading: false,

        loc_vs_edict_counts_data: null,
        loc_vs_edict_counts_is_loading: false,

    },
    mutations: {
        set_last_days(state, last_days) {
            state.last_days = last_days;
        },
        set_count_and_team_of_dirs_data(state, data) {
            state.count_and_team_of_dirs_data = data;
        },
        set_count_and_team_is_loading(state, is_loading) {
            state.count_and_team_is_loading = is_loading;
        },
        set_loc_vs_edit_counts_data(state, data) {
          state.loc_vs_edict_counts_data = data;
        },
        set_loc_vs_edit_counts_is_loading(state, is_loading) {
            state.loc_vs_edict_counts_is_loading = is_loading;
        }
    },
    getters: {
        get_count_and_team_of_dirs_dataframe(state) {
            let data = state.count_and_team_of_dirs_data;
            if(data)
                return new DataFrame(data);
            return null;
        },
        get_loc_vs_edit_counts_dataframe(state) {
            let data = state.loc_vs_edict_counts_data;
            if(data)
                return new DataFrame(data);
            return null;
        },
    },
    actions: {
        load_count_and_team_of_dirs(context) {
            context.commit('set_count_and_team_is_loading', true);

            const branch = context.rootState.common.current_branch;
            let url = `${API_BASE_PATH}/overview/count_and_team_of_dirs/${btoa(branch)}`;
            let request = axios.get(url, {params: {last_days: context.state.last_days}}).then(response => {
                context.commit('set_count_and_team_of_dirs_data', JSON.parse(response.data));
                context.commit('set_count_and_team_is_loading', false);
            });
            return request;
        },
        load_loc_vs_edit_counts(context) {
            context.commit('set_loc_vs_edit_counts_is_loading', true);

            const branch = context.rootState.common.current_branch;
            let url = `${API_BASE_PATH}/overview/loc_vs_edit_counts/${btoa(branch)}`;
            let request = axios.get(url, {params: {last_days: context.state.last_days}}).then(response => {
                context.commit('set_loc_vs_edit_counts_data', JSON.parse(response.data));
                context.commit('set_loc_vs_edit_counts_is_loading', false);
            });
            return request;
        }
    }
};