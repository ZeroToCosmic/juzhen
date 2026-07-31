function normalizeProfile(raw = {}) {
  return {
    profile_id: String(raw.profile_id || ""),
    profile_no: String(raw.profile_no || ""),
    name: String(raw.name || ""),
    group_id: String(raw.group_id || ""),
    group_name: String(raw.group_name || ""),
    platform: String(raw.platform || ""),
    username: String(raw.username || ""),
  };
}

function summarizeProfiles(profiles = [], openedProfiles = []) {
  const openedIds = new Set(
    openedProfiles
      .map((profile) => profile.user_id || profile.profile_id)
      .filter(Boolean),
  );
  const byGroup = {};

  for (const profile of profiles) {
    const groupName = profile.group_name || "Ungrouped";
    byGroup[groupName] = (byGroup[groupName] || 0) + 1;
  }

  return {
    total: profiles.length,
    opened: openedIds.size,
    byGroup,
  };
}

function buildStartOptions(options = {}) {
  const startOptions = {};

  if (options.profile_id) {
    startOptions.profile_id = String(options.profile_id);
  } else if (options.profile_no) {
    startOptions.profile_no = String(options.profile_no);
  } else {
    throw new Error("profile_id or profile_no is required");
  }

  startOptions.headless = options.headless || "0";
  startOptions.last_opened_tabs = options.last_opened_tabs || "0";

  return startOptions;
}

function createAdsPowerAdapter(client) {
  if (!client) {
    throw new Error("AdsPower client is required");
  }

  return {
    async listProfiles(options = {}) {
      const response = await client.getBrowserList(options);
      return (response.list || []).map(normalizeProfile);
    },

    async listOpened() {
      const response = await client.getOpenedBrowser();
      return response.list || [];
    },

    async summarize(options = { limit: 200, page: 1 }) {
      const profiles = await this.listProfiles(options);
      const opened = await this.listOpened();
      return summarizeProfiles(profiles, opened);
    },

    async openProfile(options) {
      return client.openBrowser(buildStartOptions(options));
    },

    async closeProfile(options = {}) {
      const closeOptions = {};

      if (options.profile_id) {
        closeOptions.profile_id = String(options.profile_id);
      } else if (options.profile_no) {
        closeOptions.profile_no = String(options.profile_no);
      } else {
        throw new Error("profile_id or profile_no is required");
      }

      return client.closeBrowser(closeOptions);
    },
  };
}

module.exports = {
  buildStartOptions,
  createAdsPowerAdapter,
  normalizeProfile,
  summarizeProfiles,
};
