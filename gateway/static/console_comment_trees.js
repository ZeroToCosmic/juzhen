(function (root, factory) {
  "use strict";
  const api = factory(root);
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.ConsoleCommentTrees = api;
  if (root?.document?.getElementById("console-comment-trees")) api.boot(root);
})(typeof globalThis !== "undefined" ? globalThis : this, function (root) {
  "use strict";

  const API = "/api/browser-v2";
  const ACTIONS = ["disable", "enable", "delete"];
  const MODE_LABELS = {independent: "独立评论", threaded: "盖楼回复"};

  function resolveEditor(injected) {
    if (injected) return injected;
    if (root && root.CommentTreeEditor) return root.CommentTreeEditor;
    if (typeof require === "function") return require("./comment_tree_editor");
    throw new Error("CommentTreeEditor is required");
  }

  function newTemplateDraft(editor) {
    return resolveEditor(editor).createDraft(undefined, "threaded");
  }

  function editableTemplate(detail) {
    const modes = Array.isArray(detail && detail.supported_modes) ? detail.supported_modes : [];
    const steps = Array.isArray(detail && detail.steps) ? detail.steps : [];
    return modes.length === 1 && steps.length > 0 && steps.every(function (step) { return step && step.content_source === "fixed"; });
  }

  function draftFromTemplate(detail) {
    const source = detail || {};
    return {
      name: source.name || "",
      description: source.description || "",
      language: source.language || "",
      tags: Array.isArray(source.tags) ? source.tags.slice() : [],
      mode: source.supported_modes[0],
      source: "manual",
      advanced: false,
      editingTemplateId: source.id,
      expectedRevision: source.revision,
      nodes: (Array.isArray(source.steps) ? source.steps : []).map(function (step) {
        return {
          id: step.id,
          label: step.label || "",
          text: step.fixed_text || "",
          parentId: step.parent_step_id || null,
          requiredProfileTags: Array.isArray(step.required_profile_tags) ? step.required_profile_tags.slice() : [],
          excludedProfileTags: Array.isArray(step.excluded_profile_tags) ? step.excluded_profile_tags.slice() : [],
          language: step.language || "",
        };
      }),
    };
  }

  function validationText(errors) {
    const labels = {
      tree_name_missing: "请填写评论树名称", tree_name_invalid: "评论树名称不能超过 100 个字符",
      tree_size_invalid: "评论数量应为 1 至 100 条", root_count_invalid: "盖楼回复必须且只能有一条楼主评论",
      independent_parent_invalid: "独立评论不能设置回复关系", comment_text_missing: "请填写每条评论文案",
      comment_text_too_long: "单条评论不能超过 2200 个字符", parent_not_found: "回复目标不存在",
      cycle_detected: "回复关系不能形成循环", node_id_missing: "评论节点无效", node_id_duplicate: "评论节点重复",
    };
    return (errors || []).map(function (item) { return labels[item.code] || "评论树内容无效"; })
      .filter(function (value, index, values) { return values.indexOf(value) === index; }).join("；");
  }

  function importErrorText(error) {
    const labels = {
      tree_name_missing: "缺少评论树名称", tree_name_invalid: "评论树名称无效", tree_empty: "评论树不能为空",
      node_no_missing: "缺少节点序号", node_no_invalid: "节点序号无效", duplicate_node_no: "节点序号重复",
      comment_text_missing: "缺少评论文案", comment_text_too_long: "评论文案过长", parent_not_found: "找不到回复目标",
      root_count_invalid: "盖楼回复必须且只能有一条楼主评论", cycle_detected: "回复关系不能形成循环",
      tree_too_large: "评论树超过 100 条", import_tree_failed: "导入评论树失败", template_invalid: "评论树内容无效",
    };
    const message = labels[(error || {}).code] || "导入内容无效";
    const row = Number((error || {}).row);
    return Number.isInteger(row) && row > 0 ? "第 " + row + " 行：" + message : message;
  }

  function importCommitPayload(trees) {
    return {trees: (Array.isArray(trees) ? trees : []).filter(function (tree) {
      return tree && tree.valid === true && tree.selected === true;
    }).map(function (tree) {
      return {
        name: String(tree.name || "").trim(),
        nodes: (Array.isArray(tree.nodes) ? tree.nodes : []).map(function (node) {
          const nodeNo = node.node_no;
          const parentNo = node.parent_node_no;
          return {
            node_no: String(nodeNo === null || nodeNo === undefined ? "" : nodeNo),
            parent_node_no: parentNo === null || parentNo === undefined || parentNo === "" ? null : String(parentNo),
            text: String(node.text || ""),
          };
        }),
      };
    })};
  }

  function importRequestError(status, fallback) {
    if (status === 403) return "当前账号无权导入评论树。";
    if (status === 413) return "Excel 文件过大，请缩小文件后重试。";
    if (status === 422) return "Excel 文件或导入内容无效，请检查后重试。";
    if (status >= 500) return "评论树导入服务暂时不可用，请稍后手动重试。";
    return fallback;
  }

  function formatTime(value) {
    if (!value) return "—";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "—";
    const parts = new Intl.DateTimeFormat("zh-CN", {
      timeZone: "Asia/Shanghai",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hourCycle: "h23",
    }).formatToParts(date).reduce(function (result, part) {
      result[part.type] = part.value;
      return result;
    }, {});
    return parts.year + "-" + parts.month + "-" + parts.day + " " + parts.hour + ":" + parts.minute;
  }

  function modeText(modes) {
    const labels = (Array.isArray(modes) ? modes : [])
      .map(function (mode) { return MODE_LABELS[mode]; })
      .filter(Boolean);
    return labels.length ? labels.join("、") : "—";
  }

  function templateSummary(template) {
    const source = template || {};
    const revision = Number(source.revision);
    const summary = {
      name: String(source.name || "未命名评论树"),
      modeLabel: modeText(source.supported_modes),
      statusLabel: source.enabled === true ? "启用" : "停用",
      revisionLabel: Number.isInteger(revision) && revision >= 0 ? "v" + revision : "—",
      updatedLabel: formatTime(source.updated_at),
    };
    Object.defineProperty(summary, "template", {value: source, enumerable: false});
    return summary;
  }

  function createListModel(state) {
    const filters = state?.filters || {};
    const query = String(filters.query || "").trim().toLocaleLowerCase("zh-CN");
    const mode = String(filters.mode || "all");
    const status = String(filters.status || "all");
    const filtered = (Array.isArray(state?.templates) ? state.templates : []).filter(function (template) {
      if (query && !String(template.name || "").toLocaleLowerCase("zh-CN").includes(query)) return false;
      if (mode !== "all" && !(Array.isArray(template.supported_modes) && template.supported_modes.includes(mode))) return false;
      if (status === "enabled" && template.enabled !== true) return false;
      if (status === "disabled" && template.enabled === true) return false;
      return true;
    });
    return {
      enabled: filtered.filter(function (item) { return item.enabled === true; }).map(templateSummary),
      disabled: filtered.filter(function (item) { return item.enabled !== true; }).map(templateSummary),
    };
  }

  function unwrapList(result) {
    const envelope = result && result.data;
    return envelope && Array.isArray(envelope.data) ? envelope.data : [];
  }

  function errorText(status, context) {
    if (status === 403) return "当前账号无权维护评论树。";
    if (status === 404) return "评论树已不存在，已刷新列表。";
    if (status === 409) return "评论树已被其他操作更新，请根据最新版本重试。";
    if (status === 422) return "当前状态不允许执行此操作，请检查后重试。";
    if (status >= 500) return "评论树服务暂时不可用，请稍后手动重试。";
    return context || "评论树请求失败，请稍后手动重试。";
  }

  function createConsoleCommentTrees(options) {
    const opts = options || {};
    if (typeof opts.requestJson !== "function") throw new TypeError("requestJson is required");
    const renderView = typeof opts.render === "function" ? opts.render : function () {};
    const confirmDelete = typeof opts.confirm === "function" ? opts.confirm : function () { return false; };
    const editor = resolveEditor(opts.commentTreeEditor);
    const FormDataCtor = opts.FormData || (root && root.FormData);
    const state = {
      view: "list",
      templates: [],
      draft: null,
      readonlyTemplate: null,
      importDraft: null,
      filters: {query: "", mode: "all", status: "all"},
      loading: false,
      submitting: false,
      error: "",
      status: "正在加载评论树…",
    };
    let editRequestGeneration = 0;
    let activeEditRequestToken = 0;

    function render() {
      renderView(state, createListModel(state));
    }

    function cancelOpenEditRequest() {
      if (!activeEditRequestToken) return false;
      editRequestGeneration += 1;
      activeEditRequestToken = 0;
      state.loading = false;
      return true;
    }

    async function refresh(preserveError) {
      cancelOpenEditRequest();
      if (state.loading) return false;
      state.loading = true;
      if (!preserveError) state.error = "";
      state.status = "正在加载评论树…";
      render();
      try {
        const result = await opts.requestJson(API + "/comment-templates", "GET");
        if (!result || result.status !== 200) {
          state.error = errorText(result && result.status, "评论树加载失败，请稍后手动重试。");
          return false;
        }
        state.templates = unwrapList(result);
        if (!preserveError) state.error = "";
        state.status = "已加载 " + state.templates.length + " 个评论树。";
        return true;
      } catch (_error) {
        state.error = "网络连接失败，请稍后手动重试。";
        return false;
      } finally {
        state.loading = false;
        render();
      }
    }

    function validTransition(template, action) {
      if (!template || !ACTIONS.includes(action)) return false;
      if (action === "disable") return template.enabled === true;
      return template.enabled !== true;
    }

    async function refreshAfterStale(message) {
      await refresh(true);
      state.error = message;
      render();
    }

    async function transition(template, action) {
      if (state.loading || state.submitting || !validTransition(template, action)) return false;
      if (action === "delete" && !confirmDelete("删除后无法在界面恢复，是否继续？")) return false;
      state.submitting = true;
      state.error = "";
      state.status = ({disable: "正在停用评论树…", enable: "正在启用评论树…", delete: "正在删除评论树…"})[action];
      render();
      try {
        const path = API + "/comment-templates/" + encodeURIComponent(template.id) + "/" + action;
        const result = await opts.requestJson(path, "POST", {expected_revision: template.revision});
        if (result && result.status === 200) {
          const loaded = await refresh();
          if (loaded) state.status = ({disable: "评论树已停用。", enable: "评论树已启用。", delete: "评论树已删除。"})[action];
          return loaded;
        }
        const status = result && result.status;
        const message = errorText(status, "评论树操作失败，请稍后手动重试。");
        state.error = message;
        if (status === 404 || status === 409) await refreshAfterStale(message);
        return false;
      } catch (_error) {
        state.error = "网络连接失败，请稍后手动重试。";
        return false;
      } finally {
        state.submitting = false;
        render();
      }
    }

    function setFilter(name, value) {
      if (name === "query") state.filters.query = String(value || "");
      if (name === "mode" && ["all", "independent", "threaded"].includes(value)) state.filters.mode = value;
      if (name === "status" && ["all", "enabled", "disabled"].includes(value)) state.filters.status = value;
      render();
    }

    function resetFilters() {
      state.filters = {query: "", mode: "all", status: "all"};
      render();
    }

    function openCreate() {
      cancelOpenEditRequest();
      if (state.loading || state.submitting) return false;
      state.view = "editor";
      state.draft = newTemplateDraft(editor);
      state.readonlyTemplate = null;
      state.error = "";
      state.status = "正在新建评论树。";
      render();
      return true;
    }

    function openImport() {
      cancelOpenEditRequest();
      if (state.loading || state.submitting) return false;
      state.view = "import";
      state.error = "";
      state.status = "请选择 .xlsx 文件进行预览。";
      render();
      return true;
    }

    function closeWorkspace() {
      cancelOpenEditRequest();
      if (state.loading || state.submitting) return false;
      state.view = "list";
      state.error = "";
      state.status = "评论树列表。";
      render();
      return true;
    }

    async function openEdit(template) {
      cancelOpenEditRequest();
      if (state.loading || state.submitting || !template) return false;
      const requestToken = ++editRequestGeneration;
      activeEditRequestToken = requestToken;
      state.loading = true;
      state.error = "";
      state.status = "正在读取评论树详情…";
      render();
      try {
        const result = await opts.requestJson(API + "/comment-templates/" + encodeURIComponent(template.id), "GET");
        if (requestToken !== activeEditRequestToken) return false;
        if (!result || result.status !== 200) {
          const status = result && result.status;
          const message = errorText(status, "无法读取评论树详情，请稍后手动重试。");
          state.error = message;
          if (status === 404) {
            state.view = "list";
            state.loading = false;
            await refreshAfterStale(message);
          }
          return false;
        }
        const detail = result.data && result.data.data ? result.data.data : null;
        if (!editableTemplate(detail)) {
          state.readonlyTemplate = detail;
          state.view = "editor";
          state.status = (detail && Array.isArray(detail.supported_modes) && detail.supported_modes.length !== 1)
            ? "支持多种模式的评论树为只读。" : "文案库评论树为只读。";
          return false;
        }
        state.readonlyTemplate = null;
        state.draft = draftFromTemplate(detail);
        state.view = "editor";
        state.status = "评论树详情已加载。";
        return true;
      } catch (_error) {
        if (requestToken !== activeEditRequestToken) return false;
        state.error = "网络连接失败，请稍后手动重试。";
        return false;
      } finally {
        if (requestToken === activeEditRequestToken) {
          activeEditRequestToken = 0;
          state.loading = false;
          render();
        }
      }
    }

    async function saveDraft() {
      if (state.submitting || !state.draft || state.readonlyTemplate) return false;
      const errors = editor.validate(state.draft);
      if (errors.length) {
        state.error = validationText(errors);
        render();
        return false;
      }
      const draft = state.draft;
      const editingId = draft.editingTemplateId;
      const body = editor.templatePayload(draft);
      if (editingId) body.expected_revision = draft.expectedRevision;
      state.submitting = true;
      state.error = "";
      state.status = "正在保存评论树…";
      render();
      try {
        const path = editingId ? API + "/comment-templates/" + encodeURIComponent(editingId) : API + "/comment-templates";
        const result = await opts.requestJson(path, editingId ? "PUT" : "POST", body);
        const expectedStatus = editingId ? 200 : 201;
        if (!result || result.status !== expectedStatus) {
          const status = result && result.status;
          const message = errorText(status, "保存评论树失败，请稍后手动重试。");
          state.error = message;
          if (status === 404 || status === 409) await refreshAfterStale(message);
          return false;
        }
        state.draft = null;
        state.readonlyTemplate = null;
        state.view = "list";
        const loaded = await refresh();
        if (loaded) state.status = "评论树已保存。";
        return true;
      } catch (_error) {
        state.error = "网络连接失败，请稍后手动重试。";
        return false;
      } finally {
        state.submitting = false;
        render();
      }
    }

    async function previewImport(file) {
      if (state.submitting) return false;
      const name = String(file && file.name || "");
      if (!file || !/\.xlsx$/i.test(name)) {
        state.error = "请选择 .xlsx 格式的 Excel 文件。";
        render();
        return false;
      }
      if (typeof FormDataCtor !== "function") {
        state.error = "当前浏览器不支持 Excel 文件导入。";
        render();
        return false;
      }
      const form = new FormDataCtor();
      form.append("file", file);
      state.submitting = true;
      state.error = "";
      state.status = "正在预览 Excel 文件…";
      render();
      try {
        const result = await opts.requestJson(API + "/comment-template-imports/preview", "POST", form);
        if (!result || result.status !== 200) {
          state.error = importRequestError(result && result.status, "Excel 文件预览失败，请检查文件后重试。");
          return false;
        }
        const data = result.data && result.data.data ? result.data.data : {};
        state.importDraft = {
          trees: (Array.isArray(data.trees) ? data.trees : []).map(function (tree) {
            return Object.assign({}, tree, {selected: tree.valid === true});
          }),
          summary: data.summary || {},
        };
        state.status = "Excel 预览完成。";
        return true;
      } catch (_error) {
        state.error = "网络连接失败，Excel 文件未上传，请稍后手动重试。";
        return false;
      } finally {
        state.submitting = false;
        render();
      }
    }

    function setImportSelection(index, selected) {
      const trees = state.importDraft && state.importDraft.trees;
      if (!Array.isArray(trees) || !trees[index] || trees[index].valid !== true) return false;
      trees[index] = Object.assign({}, trees[index], {selected: selected === true});
      render();
      return true;
    }

    async function commitImport() {
      if (state.submitting) return false;
      const body = importCommitPayload(state.importDraft && state.importDraft.trees);
      if (!body.trees.length) {
        state.error = "请至少选择一棵有效评论树。";
        render();
        return false;
      }
      state.submitting = true;
      state.error = "";
      state.status = "正在导入评论树…";
      render();
      try {
        const result = await opts.requestJson(API + "/comment-template-imports", "POST", body);
        if (!result || result.status !== 201) {
          const status = result && result.status;
          const message = importRequestError(status, status === 409 ? "导入数据已过期，请根据最新列表重试。" : "评论树导入失败，请稍后手动重试。");
          state.error = message;
          if (status === 409) await refreshAfterStale(message);
          return false;
        }
        const data = result.data && result.data.data ? result.data.data : {};
        const created = Array.isArray(data.created) ? data.created : [];
        const rejected = Array.isArray(data.rejected) ? data.rejected : [];
        if (rejected.length) {
          const rejectedByName = new Map(rejected.map(function (item) { return [item.name, item.errors || []]; }));
          state.importDraft = Object.assign({}, state.importDraft, {
            trees: state.importDraft.trees.filter(function (tree) { return rejectedByName.has(String(tree.name || "").trim()); }).map(function (tree) {
              const name = String(tree.name || "").trim();
              return Object.assign({}, tree, {name, valid: false, selected: false, errors: rejectedByName.get(name)});
            }),
          });
        } else {
          state.importDraft = null;
        }
        if (created.length) await refresh();
        if (created.length && !rejected.length) {
          state.view = "list";
          state.status = "评论树导入成功。";
          return true;
        }
        state.view = "import";
        state.error = created.length ? "部分评论树导入成功，请处理其余失败项。" : "评论树未导入，请处理失败项。";
        return created.length > 0;
      } catch (_error) {
        state.error = "网络连接失败，导入草稿已保留，请稍后手动重试。";
        return false;
      } finally {
        state.submitting = false;
        render();
      }
    }

    function confirmModeChange() {
      return confirmDelete("切换模式会重置现有回复关系，是否继续？");
    }

    function confirmRemoveDescendants() {
      return confirmDelete("删除该评论会同时删除所有后代回复，是否继续？");
    }

    return {
      state,
      init: refresh,
      refresh,
      transition,
      setFilter,
      resetFilters,
      openCreate,
      openImport,
      openEdit,
      saveDraft,
      previewImport,
      setImportSelection,
      commitImport,
      confirmModeChange,
      confirmRemoveDescendants,
      closeWorkspace,
    };
  }

  function appendCell(document, row, value, label) {
    const cell = document.createElement("td");
    cell.textContent = value;
    cell.dataset.label = label;
    row.append(cell);
    return cell;
  }

  function appendAction(document, cell, label, action, disabled) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "console-button";
    button.textContent = label;
    button.disabled = disabled;
    button.addEventListener("click", action);
    cell.append(button);
  }

  function renderRows(document, body, rows, enabledGroup, state, controller) {
    if (!body) return;
    body.replaceChildren();
    rows.forEach(function (summary) {
      const row = document.createElement("tr");
      const labels = ["评论树", "支持模式", "状态", "版本", "最近更新", "操作"];
      appendCell(document, row, summary.name, labels[0]);
      appendCell(document, row, summary.modeLabel, labels[1]);
      appendCell(document, row, summary.statusLabel, labels[2]);
      appendCell(document, row, summary.revisionLabel, labels[3]);
      appendCell(document, row, summary.updatedLabel, labels[4]);
      const actions = appendCell(document, row, "", labels[5]);
      actions.className = "action";
      const busy = state.loading || state.submitting;
      if (enabledGroup) {
        appendAction(document, actions, "编辑", function () { controller.openEdit(summary.template); }, busy);
        appendAction(document, actions, "停用", function () { controller.transition(summary.template, "disable"); }, busy);
      } else {
        appendAction(document, actions, "启用", function () { controller.transition(summary.template, "enable"); }, busy);
        appendAction(document, actions, "删除", function () { controller.transition(summary.template, "delete"); }, busy);
      }
      body.append(row);
    });
  }

  function renderDom(document, state, model, controller) {
    const byId = function (id) { return document.getElementById(id); };
    const root = byId("console-comment-trees");
    if (!root) return;
    root.dataset.view = state.view;
    const busy = state.loading || state.submitting;
    ["comment-trees-refresh", "comment-trees-create", "comment-trees-import", "comment-tree-editor-back", "comment-tree-import-back", "comment-tree-import-preview"].forEach(function (id) {
      const button = byId(id);
      if (button) button.disabled = busy;
    });
    ["list", "editor", "import"].forEach(function (view) {
      const workspace = byId("comment-tree-" + view + "-workspace");
      if (workspace) workspace.hidden = state.view !== view;
    });
    const search = byId("comment-tree-search");
    const mode = byId("comment-tree-mode-filter");
    const statusFilter = byId("comment-tree-status-filter");
    if (search && search.value !== state.filters.query) search.value = state.filters.query;
    if (mode) mode.value = state.filters.mode;
    if (statusFilter) statusFilter.value = state.filters.status;
    renderRows(document, byId("comment-tree-enabled-body"), model.enabled, true, state, controller);
    renderRows(document, byId("comment-tree-disabled-body"), model.disabled, false, state, controller);
    const enabledEmpty = byId("comment-tree-enabled-empty");
    const disabledEmpty = byId("comment-tree-disabled-empty");
    if (enabledEmpty) enabledEmpty.hidden = model.enabled.length > 0;
    if (disabledEmpty) disabledEmpty.hidden = model.disabled.length > 0;
    const status = byId("comment-trees-status");
    if (status) {
      status.textContent = state.error || state.status;
      status.classList.toggle("error", Boolean(state.error));
    }
    const editorTitle = byId("comment-tree-editor-title");
    if (editorTitle) {
      editorTitle.textContent = state.readonlyTemplate
        ? "查看评论树"
        : (state.draft && state.draft.editingTemplateId ? "编辑评论树" : "新建评论树");
    }
    const editorHost = byId("comment-tree-editor-host");
    if (editorHost && state.view === "editor") {
      editorHost.replaceChildren();
      if (state.readonlyTemplate) {
        const note = document.createElement("p");
        note.textContent = "评论树“" + String(state.readonlyTemplate.name || "未命名评论树") + "”为只读，不能在此修改。";
        editorHost.append(note);
      } else if (state.draft) {
        const editor = resolveEditor();
        editor.render({
          document,
          container: editorHost,
          draft: state.draft,
          onDraftChange: function (draft) { state.draft = draft; },
          onSave: function () { controller.saveDraft(); },
          confirmModeChange: controller.confirmModeChange,
          confirmRemoveDescendants: controller.confirmRemoveDescendants,
        });
      }
    }
    const importPreview = byId("comment-tree-import-preview-list");
    if (importPreview && state.view === "import") {
      importPreview.replaceChildren();
      const trees = state.importDraft && Array.isArray(state.importDraft.trees) ? state.importDraft.trees : [];
      if (!trees.length) {
        const empty = document.createElement("p");
        empty.textContent = "尚未生成 Excel 预览。";
        importPreview.append(empty);
      }
      trees.forEach(function (tree, index) {
        const card = document.createElement("article");
        const label = document.createElement("label");
        const checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.checked = tree.valid === true && tree.selected === true;
        checkbox.disabled = tree.valid !== true || state.submitting;
        checkbox.addEventListener("change", function () { controller.setImportSelection(index, checkbox.checked); });
        label.append(checkbox);
        const name = document.createElement("strong");
        name.textContent = String(tree.name || "未命名评论树");
        label.append(name);
        card.append(label);
        const count = document.createElement("p");
        count.textContent = "节点数：" + (Array.isArray(tree.nodes) ? tree.nodes.length : 0);
        card.append(count);
        const errors = Array.isArray(tree.errors) ? tree.errors : [];
        if (errors.length) {
          const list = document.createElement("ul");
          errors.forEach(function (error) {
            const item = document.createElement("li");
            item.textContent = importErrorText(error);
            list.append(item);
          });
          card.append(list);
        }
        importPreview.append(card);
      });
    }
    const importCommit = byId("comment-tree-import-commit");
    if (importCommit) {
      const trees = state.importDraft && Array.isArray(state.importDraft.trees) ? state.importDraft.trees : [];
      importCommit.disabled = busy || !trees.some(function (tree) { return tree.valid === true && tree.selected === true; });
    }
  }

  function bindDom(document, controller) {
    const byId = function (id) { return document.getElementById(id); };
    byId("comment-trees-refresh")?.addEventListener("click", function () { controller.refresh(); });
    byId("comment-trees-create")?.addEventListener("click", controller.openCreate);
    byId("comment-trees-import")?.addEventListener("click", controller.openImport);
    byId("comment-tree-editor-back")?.addEventListener("click", controller.closeWorkspace);
    byId("comment-tree-import-back")?.addEventListener("click", controller.closeWorkspace);
    byId("comment-tree-import-preview")?.addEventListener("click", function () {
      const input = byId("comment-tree-import-file");
      controller.previewImport(input && input.files ? input.files[0] : null);
    });
    byId("comment-tree-import-commit")?.addEventListener("click", function () { controller.commitImport(); });
    byId("comment-tree-search")?.addEventListener("input", function (event) { controller.setFilter("query", event.currentTarget.value); });
    byId("comment-tree-mode-filter")?.addEventListener("change", function (event) { controller.setFilter("mode", event.currentTarget.value); });
    byId("comment-tree-status-filter")?.addEventListener("change", function (event) { controller.setFilter("status", event.currentTarget.value); });
    byId("comment-tree-filter-reset")?.addEventListener("click", controller.resetFilters);
    byId("comment-tree-filters")?.addEventListener("submit", function (event) { event.preventDefault(); });
  }

  async function requestJson(win, url, method, body) {
    const verb = String(method || "GET").toUpperCase();
    const options = {method: verb, credentials: "same-origin"};
    if (body !== undefined && verb !== "GET" && verb !== "HEAD") {
      const isFormData = typeof win.FormData === "function" && body instanceof win.FormData;
      if (isFormData) options.body = body;
      else {
        options.headers = {"Content-Type": "application/json"};
        options.body = JSON.stringify(body);
      }
    }
    const response = await win.fetch(url, options);
    let data;
    try {
      data = await response.json();
    } catch (_error) {
      data = {error: {code: "invalid_response", message: "服务返回格式无效"}};
    }
    return {status: response.status, data};
  }

  function boot(win) {
    const browser = win || root;
    const document = browser && browser.document;
    if (!document || !document.getElementById("console-comment-trees")) return null;
    if (browser.__consoleCommentTreesController) return browser.__consoleCommentTreesController;
    const managementRequest = browser.ManagementFetch && typeof browser.ManagementFetch.requestJson === "function"
      ? browser.ManagementFetch.requestJson.bind(browser.ManagementFetch)
      : requestJson.bind(null, browser);
    let controller;
    controller = createConsoleCommentTrees({
      requestJson: managementRequest,
      confirm: typeof browser.confirm === "function" ? browser.confirm.bind(browser) : function () { return false; },
      render: function (state, model) { renderDom(document, state, model, controller); },
    });
    bindDom(document, controller);
    browser.__consoleCommentTreesController = controller;
    controller.init();
    return controller;
  }

  return {
    createConsoleCommentTrees,
    createListModel,
    templateSummary,
    newTemplateDraft,
    editableTemplate,
    draftFromTemplate,
    importErrorText,
    importCommitPayload,
    renderDom,
    requestJson,
    boot,
  };
});
