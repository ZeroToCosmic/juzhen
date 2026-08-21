(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.CommentTreeEditor = factory();
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  var MAX_NODES = 100;
  var MAX_TEXT_LENGTH = 2200;

  function browserId() {
    if (typeof crypto === "undefined" || typeof crypto.randomUUID !== "function") throw new Error("uuid_factory_required");
    return crypto.randomUUID();
  }

  function cloneNodes(nodes) {
    return (Array.isArray(nodes) ? nodes : []).map(function (node) { return Object.assign({}, node); });
  }

  function cloneDraft(draft, nodes) {
    return Object.assign({}, draft || {}, {nodes: nodes === undefined ? cloneNodes(draft && draft.nodes) : nodes});
  }

  function createDraft(idFactory, mode) {
    var makeId = idFactory || browserId;
    return {name: "", mode: mode || "threaded", source: "manual", advanced: false, nodes: [{id: makeId(), text: "", parentId: null}]};
  }

  function addReply(draft, idFactory) {
    var makeId = idFactory || browserId;
    var nodes = cloneNodes(draft && draft.nodes);
    var previous = nodes[nodes.length - 1] || null;
    nodes.push({id: makeId(), text: "", parentId: draft && draft.mode === "threaded" && previous ? previous.id : null});
    return cloneDraft(draft, nodes);
  }

  function descendantsOf(nodes, nodeId) {
    var descendants = new Set([nodeId]);
    var changed = true;
    while (changed) {
      changed = false;
      nodes.forEach(function (node) {
        if (node.parentId && descendants.has(node.parentId) && !descendants.has(node.id)) {
          descendants.add(node.id);
          changed = true;
        }
      });
    }
    return descendants;
  }

  function removeNode(draft, nodeId, options) {
    var nodes = cloneNodes(draft && draft.nodes);
    if (!nodes.some(function (node) { return node.id === nodeId; })) return cloneDraft(draft, nodes);
    var descendants = descendantsOf(nodes, nodeId);
    if (descendants.size > 1 && !(options && options.removeDescendants)) throw new Error("node_has_descendants");
    return cloneDraft(draft, nodes.filter(function (node) { return !descendants.has(node.id); }));
  }

  function moveNode(draft, nodeId, direction) {
    var nodes = cloneNodes(draft && draft.nodes);
    var index = nodes.findIndex(function (node) { return node.id === nodeId; });
    var target = index + Number(direction || 0);
    if (index < 0 || target < 0 || target >= nodes.length) return cloneDraft(draft, nodes);
    var current = nodes[index];
    nodes[index] = nodes[target];
    nodes[target] = current;
    return cloneDraft(draft, nodes);
  }

  function isDescendant(nodes, possibleDescendantId, nodeId) {
    var current = nodes.find(function (node) { return node.id === possibleDescendantId; });
    var seen = new Set();
    while (current && current.parentId && !seen.has(current.id)) {
      if (current.parentId === nodeId) return true;
      seen.add(current.id);
      current = nodes.find(function (node) { return node.id === current.parentId; });
    }
    return false;
  }

  function setParent(draft, nodeId, parentId) {
    var nodes = cloneNodes(draft && draft.nodes);
    var node = nodes.find(function (candidate) { return candidate.id === nodeId; });
    if (!node) throw new Error("node_not_found");
    if (draft && draft.mode === "independent" && parentId) throw new Error("independent_parent_invalid");
    if (parentId !== null && parentId !== undefined && parentId !== "") {
      if (!nodes.some(function (candidate) { return candidate.id === parentId; })) throw new Error("parent_not_found");
      if (parentId === nodeId || isDescendant(nodes, parentId, nodeId)) throw new Error("cycle_detected");
      node.parentId = parentId;
    } else {
      node.parentId = null;
    }
    return cloneDraft(draft, nodes);
  }

  function cycleErrors(nodes) {
    var byId = new Map();
    nodes.forEach(function (node) { if (node && node.id) byId.set(node.id, node); });
    var errors = [];
    nodes.forEach(function (node, index) {
      var current = node;
      var seen = new Set();
      while (current && current.parentId && byId.has(current.parentId)) {
        if (seen.has(current.id)) {
          errors.push({code: "cycle_detected", index: index});
          break;
        }
        seen.add(current.id);
        current = byId.get(current.parentId);
      }
    });
    return errors;
  }

  function validate(draft) {
    var safeDraft = draft || {};
    var nodes = Array.isArray(safeDraft.nodes) ? safeDraft.nodes : [];
    var errors = [];
    var name = String(safeDraft.name || "").trim();
    if (!name) errors.push({code: "tree_name_missing"});
    if (name.length > 100) errors.push({code: "tree_name_invalid"});
    if (nodes.length < 1 || nodes.length > MAX_NODES) errors.push({code: "tree_size_invalid"});
    var ids = new Set();
    nodes.forEach(function (node, index) {
      if (!node || !String(node.id || "").trim()) errors.push({code: "node_id_missing", index: index});
      else if (ids.has(node.id)) errors.push({code: "node_id_duplicate", index: index});
      else ids.add(node.id);
      var text = String((node && node.text) || "").trim();
      if (!text) errors.push({code: "comment_text_missing", index: index});
      if (text.length > MAX_TEXT_LENGTH) errors.push({code: "comment_text_too_long", index: index});
      if (node && node.parentId && !ids.has(node.parentId) && !nodes.some(function (candidate) { return candidate && candidate.id === node.parentId; })) errors.push({code: "parent_not_found", index: index});
    });
    var roots = nodes.filter(function (node) { return node && !node.parentId; });
    if (safeDraft.mode === "threaded" && roots.length !== 1) errors.push({code: "root_count_invalid"});
    if (safeDraft.mode === "independent" && roots.length !== nodes.length) errors.push({code: "independent_parent_invalid"});
    return errors.concat(cycleErrors(nodes));
  }

  function templatePayload(draft) {
    var safeDraft = draft || {};
    var mode = safeDraft.mode === "independent" ? "independent" : "threaded";
    return {
      name: String(safeDraft.name || "").trim(),
      description: String(safeDraft.description || ""),
      supported_modes: [mode],
      language: String(safeDraft.language || ""),
      tags: Array.isArray(safeDraft.tags) ? safeDraft.tags.slice() : [],
      steps: cloneNodes(safeDraft.nodes).map(function (node, index) {
        var parentId = mode === "threaded" && node.parentId ? node.parentId : null;
        return {
          id: node.id,
          label: String(node.label || (parentId ? "回复 " + (index + 1) : "楼主评论")),
          content_source: "fixed",
          fixed_text: String(node.text || "").trim(),
          content_library_id: "",
          content_item_id: "",
          parent_step_id: parentId,
          required_profile_tags: Array.isArray(node.requiredProfileTags) ? node.requiredProfileTags.slice() : [],
          excluded_profile_tags: Array.isArray(node.excludedProfileTags) ? node.excludedProfileTags.slice() : [],
          language: String(node.language || ""),
        };
      }),
    };
  }

  function element(document, tag, text, className) {
    var node = document.createElement(tag);
    if (text !== undefined && text !== null) node.textContent = text;
    if (className) node.className = className;
    return node;
  }

  function draftWithText(draft, nodeId, text) {
    return cloneDraft(draft, cloneNodes(draft.nodes).map(function (node) { return node.id === nodeId ? Object.assign({}, node, {text: text}) : node; }));
  }

  function switchMode(draft, mode) {
    if (mode === "independent") return cloneDraft(Object.assign({}, draft, {mode: mode}), cloneNodes(draft.nodes).map(function (node) { return Object.assign({}, node, {parentId: null}); }));
    var previousId = null;
    return cloneDraft(Object.assign({}, draft, {mode: "threaded"}), cloneNodes(draft.nodes).map(function (node, index) {
      var next = Object.assign({}, node, {parentId: index === 0 ? null : previousId});
      previousId = node.id;
      return next;
    }));
  }

  function render(options) {
    var settings = options || {};
    var document = settings.document;
    var container = settings.container;
    if (!document || !container) throw new Error("document_and_container_required");
    var draft = settings.draft || createDraft(settings.idFactory);
    function publish(next) {
      draft = next;
      if (typeof settings.onDraftChange === "function") settings.onDraftChange(next);
    }
    function redraw() { render(Object.assign({}, settings, {draft: draft})); }
    function action(next) { publish(next); redraw(); }

    container.textContent = "";
    var layout = element(document, "div", null, "comment-tree-layout");
    var editor = element(document, "section", null, "comment-tree-editor");
    var nameLabel = element(document, "label", "评论树名称");
    var name = element(document, "input");
    name.type = "text";
    name.value = draft.name || "";
    name.maxLength = 100;
    name.addEventListener("input", function () { publish(Object.assign({}, draft, {name: name.value})); });
    nameLabel.append(name);
    editor.append(nameLabel);

    var modeLabel = element(document, "label", "模式");
    var mode = element(document, "select");
    [["independent", "独立评论"], ["threaded", "盖楼回复"]].forEach(function (choice) {
      var option = element(document, "option", choice[1]);
      option.value = choice[0];
      option.selected = draft.mode === choice[0];
      mode.append(option);
    });
    mode.addEventListener("change", function () {
      if (mode.value === "independent" && draft.mode !== "independent" && draft.nodes.some(function (node) { return node.parentId; })) {
        if (typeof settings.confirmModeChange !== "function" || !settings.confirmModeChange()) { mode.value = draft.mode; return; }
      }
      action(switchMode(draft, mode.value));
    });
    modeLabel.append(mode);
    editor.append(modeLabel);

    if (draft.mode === "threaded") {
      var advancedLabel = element(document, "label", "启用高级分支");
      var advanced = element(document, "input");
      advanced.type = "checkbox";
      advanced.checked = Boolean(draft.advanced);
      advanced.addEventListener("change", function () { action(Object.assign({}, draft, {advanced: advanced.checked})); });
      advancedLabel.append(advanced);
      editor.append(advancedLabel);
    }

    draft.nodes.forEach(function (node, index) {
      var card = element(document, "article", null, "comment-tree-node");
      var nodeTitle = draft.mode === "independent" ? "独立评论 " + (index + 1) : (node.parentId ? "回复评论 " + (index + 1) : "楼主评论");
      card.append(element(document, "h3", nodeTitle));
      var textLabel = element(document, "label", "评论文案");
      var text = element(document, "textarea");
      text.value = node.text || "";
      text.maxLength = MAX_TEXT_LENGTH;
      text.addEventListener("input", function () { publish(draftWithText(draft, node.id, text.value)); });
      textLabel.append(text);
      card.append(textLabel);
      if (draft.mode === "threaded" && draft.advanced) {
        var parentLabel = element(document, "label", "回复哪条评论");
        var parent = element(document, "select");
        var none = element(document, "option", "作为楼主评论");
        none.value = "";
        parent.append(none);
        draft.nodes.forEach(function (candidate, candidateIndex) {
          if (candidate.id === node.id) return;
          var option = element(document, "option", candidate.parentId ? "回复评论 " + (candidateIndex + 1) : "楼主评论");
          option.value = candidate.id;
          option.selected = node.parentId === candidate.id;
          parent.append(option);
        });
        parent.addEventListener("change", function () { try { action(setParent(draft, node.id, parent.value || null)); } catch (_error) { parent.value = node.parentId || ""; } });
        parentLabel.append(parent);
        card.append(parentLabel);
      }
      [["上移", -1], ["下移", 1]].forEach(function (movement) {
        var button = element(document, "button", movement[0], "comment-tree-button");
        button.type = "button";
        button.addEventListener("click", function () { action(moveNode(draft, node.id, movement[1])); });
        card.append(button);
      });
      var remove = element(document, "button", "删除", "comment-tree-button");
      remove.type = "button";
      remove.addEventListener("click", function () {
        try { action(removeNode(draft, node.id, {removeDescendants: typeof settings.confirmRemoveDescendants === "function" && settings.confirmRemoveDescendants(node)})); }
        catch (_error) { /* Parent deletion requires the injected confirmation callback. */ }
      });
      card.append(remove);
      editor.append(card);
    });
    var add = element(document, "button", "新增评论", "comment-tree-button");
    add.type = "button";
    add.addEventListener("click", function () { action(addReply(draft, settings.idFactory)); });
    editor.append(add);
    var save = element(document, "button", "保存评论树", "comment-tree-button");
    save.type = "button";
    save.addEventListener("click", function () { if (typeof settings.onSave === "function") settings.onSave(templatePayload(draft), validate(draft)); });
    editor.append(save);
    var preview = element(document, "aside", null, "comment-tree-preview");
    preview.append(element(document, "h3", "评论树预览"));
    var previewList = element(document, "ol");
    var byId = new Map((draft.nodes || []).map(function (node) { return [node.id, node]; }));
    function summary(node) {
      var value = String((node && node.text) || "").trim();
      return value ? value.slice(0, 48) + (value.length > 48 ? "…" : "") : "未填写文案";
    }
    function depth(node) {
      var current = node;
      var seen = new Set();
      var result = 0;
      while (current && current.parentId && byId.has(current.parentId) && !seen.has(current.id)) {
        seen.add(current.id);
        result += 1;
        current = byId.get(current.parentId);
      }
      return result;
    }
    (draft.nodes || []).forEach(function (node, index) {
      var parent = node.parentId ? byId.get(node.parentId) : null;
      var title;
      if (draft.mode === "independent") title = "独立评论 " + (index + 1);
      else if (!parent) title = "楼主评论";
      else title = "第 " + depth(node) + " 层回复（父评论：" + summary(parent) + "）";
      previewList.append(element(document, "li", title));
    });
    preview.append(previewList);
    layout.append(editor, preview);
    container.append(layout);
    return {draft: draft, errors: validate(draft)};
  }

  return {createDraft: createDraft, addReply: addReply, removeNode: removeNode, moveNode: moveNode, setParent: setParent, validate: validate, templatePayload: templatePayload, render: render};
});
