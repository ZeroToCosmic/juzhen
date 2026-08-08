"""Legacy browser routes (migrated from gateway/app.py)."""

from __future__ import annotations

import copy
import json
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import requests
from flask import Blueprint, abort, current_app, jsonify, request, send_from_directory, session

from browser_element_schema import TIKTOK_COMMENT_TEMPLATE, normalize_element_definitions
from browser_strategy_config import (
    ACTION_CATALOG,
    DEFAULT_ACTION_PARAMS,
    normalize_block_strategies,
    normalize_elements,
    normalize_patterns,
)
import gateway.browser_legacy as legacy
from gateway.browser_legacy import (
    ACTIVE_BROWSER_SESSIONS,
    ACTIVE_BROWSER_SESSIONS_LOCK,
    ACTIVE_PATTERN_RECORDINGS,
    ACTIVE_PATTERN_RECORDINGS_LOCK,
    BROWSER_BATCH_TASKS,
    BROWSER_BATCH_TASKS_LOCK,
    BrowserStageError,
    PROJECT_ROOT,
)
from gateway.content_store import now_iso as content_now_iso
from gateway.settings_store import load_settings

bp = Blueprint("legacy-browser", __name__)

@bp.post("/api/browser/sync-tabs")
def sync_browser_tabs_route():
    payload = request.get_json(silent=True) or {}
    url = str(payload.get("url") or "").strip()
    if not url:
        return jsonify({"error": "url 不能为空"}), 400
    sessions = legacy.selected_browser_sessions(payload.get("windows"))
    if not sessions or any(not ws_url for _profile_id, ws_url in sessions):
        legacy.release_selected_browser_sessions(sessions)
        return jsonify({"error": "没有找到已打开窗口，请先打开并平铺窗口"}), 400

    def sync_one(item):
        profile_id, ws_url = item
        try:
            from browser_cdp import navigate_and_close_other_tabs

            return {"profile_id": profile_id, "status": "ok", **navigate_and_close_other_tabs(ws_url, url)}
        except Exception as error:
            return {"profile_id": profile_id, "status": "failed", "error": str(error)}

    try:
        with ThreadPoolExecutor(max_workers=len(sessions)) as executor:
            results = list(executor.map(sync_one, sessions))
    finally:
        legacy.release_selected_browser_sessions(sessions)
    response_payload = {"url": url, "results": results}
    legacy.record_browser_log("sync_tabs", response_payload)
    return jsonify(response_payload)

def inspect_browser_elements_response(payload, *, use_saved_elements: bool = False):
    elements = payload.get("elements") if isinstance(payload, dict) else None
    if not isinstance(elements, dict) and use_saved_elements:
        elements = load_settings().get("browser", {}).get("action_elements", {})
    try:
        elements = normalize_element_definitions(elements)
    except (TypeError, ValueError):
        return jsonify({"error": "invalid element definitions"}), 400
    if not elements:
        return jsonify({"error": "at least one element definition is required"}), 400
    try:
        sessions = legacy.selected_browser_sessions(payload.get("windows"))
    except (AttributeError, TypeError):
        return jsonify({"error": "invalid browser window selection"}), 400
    if not sessions or any(not ws_url for _profile_id, ws_url in sessions):
        legacy.release_selected_browser_sessions(sessions)
        return jsonify({"error": "没有找到已打开窗口，请先打开并平铺窗口"}), 400

    def inspect_one(item):
        profile_id, ws_url = item
        try:
            inspected = legacy.inspect_browser_elements_on_cdp(ws_url, elements)
            if not isinstance(inspected, list):
                inspected = []
            return {
                "profile_id": profile_id,
                "status": "ok",
                "elements": [
                    legacy.public_element_inspection(
                        inspected[index]
                        if index < len(inspected)
                        else {"status": "error"},
                        alias,
                        definition,
                    )
                    for index, (alias, definition) in enumerate(elements.items())
                ],
            }
        except Exception:
            return {
                "profile_id": profile_id,
                "status": "failed",
                "code": "element_inspection_failed",
                "elements": [
                    legacy.public_element_inspection(
                        {"status": "error"}, alias, definition
                    )
                    for alias, definition in elements.items()
                ],
            }

    try:
        results = [inspect_one(item) for item in sessions]
    finally:
        legacy.release_selected_browser_sessions(sessions)
    response_payload = {"results": results}
    legacy.record_browser_log("inspect_elements", response_payload)
    return jsonify(response_payload)

@bp.post("/api/browser/elements/test")
def test_browser_elements_route():
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"error": "element inspection payload must be a JSON object"}), 400
    return inspect_browser_elements_response(payload)

@bp.post("/api/browser/read-elements")
def read_browser_elements_route():
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"error": "element inspection payload must be a JSON object"}), 400
    return inspect_browser_elements_response(payload, use_saved_elements=True)

@bp.post("/api/browser/execute-strategy")
def execute_browser_strategy_route():
    app = current_app._get_current_object()
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "请求格式无效，必须是 JSON 对象"}), 400
    try:
        browser = legacy.load_persisted_strategy_state()
    except (TypeError, ValueError) as error:
        return jsonify({"error": str(error)}), 400
    elements = browser["action_elements"]
    patterns = browser["interaction_patterns"]
    strategy_id = str(payload.get("strategy_id") or "")
    target_url = legacy.get_browser_target_url(payload)
    if not legacy.is_valid_browser_url(target_url):
        return jsonify({"error": "url must be a valid http:// or https:// URL"}), 400
    strategy = next(
        (
            item
            for item in browser["block_strategies"]
            if item.get("id") == strategy_id
        ),
        None,
    )
    if not strategy:
        return jsonify({"error": "执行策略不存在，请到执行策略模块保存后再试"}), 400
    if strategy.get("status") == "needs_repair":
        detail = "; ".join(strategy.get("repair_errors") or [])
        return jsonify({"error": f"strategy needs repair before execution: {detail}"}), 400
    initial_gate = legacy.browser_strategy_gate_check(current_app, strategy["id"])
    if initial_gate.get("allowed") is not True:
        return jsonify(
            {
                "error": "strategy_paused",
                "code": "strategy_paused",
                "strategy_id": legacy._public_identifier(
                    strategy["id"],
                    "strategy",
                ),
                "reasons": initial_gate.get("reasons", []),
            }
        ), 409
    try:
        profiles = legacy.normalize_selected_browser_profiles(payload.get("windows"))
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    session_results, _layout = legacy.ensure_browser_profile_sessions(
        profiles, lease_sessions=True
    )
    ready_profile_ids = [
        item["profile_id"]
        for item in session_results
        if item.get("status") == "ready"
    ]

    def execute_one(item):
        profile_id = item["profile_id"]
        attempts = item["attempts"]
        if item.get("status") != "ready":
            return {
                "profile_id": profile_id,
                "status": "failed",
                "stage": item["stage"],
                "attempts": attempts,
                "target_url": target_url,
                "error": item.get("error", ""),
            }
        tile_error = legacy.browser_tile_error(
            _layout, profile_id, ready_profile_ids
        )
        if tile_error:
            return {
                "profile_id": profile_id,
                "status": "failed",
                "stage": "tile",
                "attempts": attempts,
                "target_url": target_url,
                "error": tile_error,
            }
        ws_url = item["ws_url"]
        try:
            from browser_strategy_runtime import (
                run_prepared_block_strategy_on_cdp,
            )

            with legacy.browser_profile_execution_reservation(profile_id):
                gate_check = (
                    lambda checked_strategy_id, action=None:
                    legacy.browser_strategy_gate_check(
                        app,
                        checked_strategy_id,
                        action,
                    )
                )
                reserved_gate = gate_check(strategy["id"])
                if reserved_gate.get("allowed") is not True:
                    raise legacy._strategy_gate_error(
                        strategy["id"],
                        reserved_gate,
                    )
                result = legacy.public_strategy_execution_result(
                    legacy._run_prepared_strategy_with_gate(
                        run_prepared_block_strategy_on_cdp,
                        (
                            ws_url,
                            target_url,
                            strategy,
                            elements,
                            patterns,
                            legacy.build_strategy_text_resolver(
                                app.config["CONTENT_DATA_DIR"]
                            ),
                        ),
                        gate_check,
                    ),
                    strategy,
                    elements,
                )
            return {
                **result,
                "profile_id": profile_id,
                "status": "ok",
                "stage": "execute_actions",
                "attempts": attempts,
                "target_url": target_url,
            }
        except Exception as error:
            return legacy.public_strategy_failure_result(
                profile_id=profile_id,
                attempts=attempts,
                target_url=target_url,
                error=error,
                strategy=strategy,
                elements=elements,
            )

    try:
        with ThreadPoolExecutor(max_workers=len(session_results)) as executor:
            results = list(executor.map(execute_one, session_results))
    finally:
        legacy.release_browser_session_results(session_results)
    response_payload = legacy.public_browser_payload(
        {
            "task_id": uuid4().hex,
            "strategy_id": legacy._public_identifier(strategy_id, "strategy"),
            "results": results,
        }
    )
    legacy.record_browser_log("execute_strategy", response_payload)
    return jsonify(response_payload)

@bp.get("/api/browser/elements")
def get_browser_elements():
    try:
        return jsonify({"elements": legacy.load_persisted_strategy_state()["action_elements"]})
    except (TypeError, ValueError) as error:
        return jsonify({"error": str(error)}), 400

@bp.get("/api/browser/elements/templates/tiktok-comment")
def get_tiktok_comment_element_template():
    return jsonify({"elements": copy.deepcopy(TIKTOK_COMMENT_TEMPLATE)})

@bp.get("/api/browser/action-catalog")
def get_browser_action_catalog():
    return jsonify(
        {
            "catalog": copy.deepcopy(ACTION_CATALOG),
            "defaults": copy.deepcopy(DEFAULT_ACTION_PARAMS),
        }
    )

@bp.put("/api/browser/elements")
def save_browser_elements():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "elements payload must be a JSON object"}), 400
    try:
        def update(browser):
            elements = normalize_elements(payload.get("elements"))
            previous_elements = browser["action_elements"]
            strategies = browser["block_strategies"]
            rename_from = payload.get("rename_from")
            renamed_to = None
            if rename_from is not None:
                if not isinstance(rename_from, str) or not rename_from.strip():
                    raise ValueError("rename_from must be a non-empty element alias")
                rename_from = rename_from.strip()
                if rename_from not in previous_elements:
                    raise ValueError("rename_from must identify an existing element")
                if rename_from in elements:
                    raise ValueError("renamed element alias must replace rename_from")
                additions = [alias for alias in elements if alias not in previous_elements]
                if len(additions) != 1:
                    raise ValueError("element rename must add exactly one replacement alias")
                renamed_to = additions[0]
                strategies = copy.deepcopy(strategies)
                for strategy in strategies:
                    for action in strategy["actions"]:
                        params = action["params"]
                        if params.get("element") == rename_from:
                            params["element"] = renamed_to

            removed = set(previous_elements) - set(elements)
            if rename_from is not None:
                removed.discard(rename_from)
            references = [
                reference
                for alias in sorted(removed)
                for reference in element_references(browser["block_strategies"], alias)
            ]
            if references:
                raise legacy._StrategyReferenceConflict("element", references)
            browser["strategy_schema_version"] = 3
            browser["action_elements"] = elements
            if renamed_to is not None:
                browser["block_strategies"] = strategies
            return browser

        browser = legacy.mutate_persisted_strategy_state(update)
        return jsonify({"elements": browser["action_elements"]})
    except _StrategyReferenceConflict as error:
        return jsonify({"error": str(error), "references": error.references}), 409
    except (TypeError, ValueError) as error:
        return jsonify({"error": str(error)}), 400

@bp.get("/api/browser/patterns")
def get_browser_patterns():
    try:
        return jsonify({"patterns": legacy.load_persisted_strategy_state()["interaction_patterns"]})
    except (TypeError, ValueError) as error:
        return jsonify({"error": str(error)}), 400

@bp.put("/api/browser/patterns")
def save_browser_patterns():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "patterns payload must be a JSON object"}), 400
    try:
        def update(browser):
            patterns = normalize_patterns(payload.get("patterns"))
            previous_ids = {pattern["id"] for pattern in browser["interaction_patterns"]}
            current_ids = {pattern["id"] for pattern in patterns}
            references = [
                reference
                for pattern_id in sorted(previous_ids - current_ids)
                for reference in pattern_references(browser["block_strategies"], pattern_id)
            ]
            if references:
                raise legacy._StrategyReferenceConflict("pattern", references)
            normalize_block_strategies(
                browser["block_strategies"],
                browser["action_elements"],
                patterns,
                allow_repair=True,
            )
            browser["strategy_schema_version"] = 3
            browser["interaction_patterns"] = patterns
            return browser

        browser = legacy.mutate_persisted_strategy_state(update)
        return jsonify({"patterns": browser["interaction_patterns"]})
    except _StrategyReferenceConflict as error:
        return jsonify({"error": str(error), "references": error.references}), 409
    except (TypeError, ValueError) as error:
        return jsonify({"error": str(error)}), 400

@bp.post("/api/browser/pattern-recordings/start")
def start_browser_pattern_recording():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "请求格式无效，必须是 JSON 对象"}), 400
    selected = payload.get("windows")
    if not isinstance(selected, list) or len(selected) != 1:
        return jsonify({"error": "录制时必须且只能选择 1 个已打开窗口"}), 400
    pattern_type = str(payload.get("type") or "").strip()
    if pattern_type not in {"mouse", "keyboard"}:
        return jsonify({"error": "录制类型必须是 mouse 或 keyboard"}), 400
    try:
        profile = legacy.normalize_selected_browser_profiles(selected)[0]
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    profile_id = profile["profile_id"]
    sessions = legacy.selected_browser_sessions([profile_id])
    if len(sessions) != 1 or not sessions[0][1]:
        legacy.release_selected_browser_sessions(sessions)
        return jsonify({"error": "没有找到选中的已打开窗口，请先打开窗口"}), 400
    _leased_profile_id, ws_url = sessions[0]
    recording_id = uuid4().hex
    reservation = {
        "profile_id": profile_id,
        "ws_url": ws_url,
        "type": pattern_type,
        "_state": "preparing",
    }
    try:
        from browser_pattern_recorder import finish_recording, prepare_recording

        with ACTIVE_PATTERN_RECORDINGS_LOCK:
            if any(
                item.get("profile_id") == profile_id
                for item in ACTIVE_PATTERN_RECORDINGS.values()
            ):
                return jsonify({"error": "该窗口已有进行中的行为录制"}), 409
            ACTIVE_PATTERN_RECORDINGS[recording_id] = reservation
        prepared = prepare_recording(ws_url, recording_id, pattern_type)
        with ACTIVE_PATTERN_RECORDINGS_LOCK:
            commit_won = ACTIVE_PATTERN_RECORDINGS.get(recording_id) is reservation
            if commit_won:
                ACTIVE_PATTERN_RECORDINGS[recording_id] = {
                    "profile_id": profile_id,
                    "ws_url": ws_url,
                    "type": pattern_type,
                }
        if not commit_won:
            try:
                finish_recording(ws_url, recording_id)
            except Exception:
                pass
            raise RuntimeError("录制上下文已失效")
        return jsonify({**prepared, "profile_id": profile_id})
    except (TypeError, ValueError, RuntimeError) as error:
        with ACTIVE_PATTERN_RECORDINGS_LOCK:
            if ACTIVE_PATTERN_RECORDINGS.get(recording_id) is reservation:
                ACTIVE_PATTERN_RECORDINGS.pop(recording_id, None)
        return jsonify({"error": str(error)}), 400
    finally:
        legacy.release_selected_browser_sessions(sessions)

def pattern_recording_context(recording_id):
    with ACTIVE_PATTERN_RECORDINGS_LOCK:
        context = ACTIVE_PATTERN_RECORDINGS.get(recording_id)
        return copy.deepcopy(context) if context else None

def forget_pattern_recording(recording_id, expected=None):
    with ACTIVE_PATTERN_RECORDINGS_LOCK:
        if expected is None or ACTIVE_PATTERN_RECORDINGS.get(recording_id) == expected:
            ACTIVE_PATTERN_RECORDINGS.pop(recording_id, None)

@bp.get("/api/browser/pattern-recordings/<recording_id>")
def get_browser_pattern_recording(recording_id):
    context = pattern_recording_context(recording_id)
    if context is None:
        return jsonify({"error": "录制上下文已失效"}), 409
    profile_id = context["profile_id"]
    ws_url = context["ws_url"]
    if not legacy.acquire_browser_session_use(profile_id, ws_url):
        forget_pattern_recording(recording_id, context)
        return jsonify({"error": "录制上下文已失效"}), 409
    try:
        from browser_pattern_recorder import read_recording

        return jsonify({**read_recording(ws_url, recording_id), "profile_id": profile_id})
    except Exception:
        forget_pattern_recording(recording_id, context)
        return jsonify({"error": "录制上下文已失效"}), 409
    finally:
        legacy.release_browser_session_use(profile_id, ws_url)

@bp.post("/api/browser/pattern-recordings/<recording_id>/stop")
def stop_browser_pattern_recording(recording_id):
    context = pattern_recording_context(recording_id)
    if context is None:
        return jsonify({"error": "录制上下文已失效"}), 409
    profile_id = context["profile_id"]
    ws_url = context["ws_url"]
    if not legacy.acquire_browser_session_use(profile_id, ws_url):
        forget_pattern_recording(recording_id, context)
        return jsonify({"error": "录制上下文已失效"}), 409
    try:
        from browser_pattern_recorder import finish_recording

        return jsonify({**finish_recording(ws_url, recording_id), "profile_id": profile_id})
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    except Exception:
        return jsonify({"error": "录制上下文已失效"}), 409
    finally:
        forget_pattern_recording(recording_id, context)
        legacy.release_browser_session_use(profile_id, ws_url)

@bp.get("/api/browser/strategies")
def get_browser_strategies():
    try:
        return jsonify({"strategies": legacy.load_persisted_strategy_state()["block_strategies"]})
    except (TypeError, ValueError) as error:
        return jsonify({"error": str(error)}), 400

@bp.put("/api/browser/strategies")
def save_browser_strategies():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "strategies payload must be a JSON object"}), 400
    dependency_swap = {
        "attempted": False,
        "applied": False,
        "previous": [],
        "candidate": [],
    }
    try:
        def update(browser):
            dependency_swap["previous"] = copy.deepcopy(
                browser["block_strategies"]
            )
            browser["strategy_schema_version"] = 3
            browser["block_strategies"] = normalize_block_strategies(
                payload.get("strategies"),
                browser["action_elements"],
                browser["interaction_patterns"],
            )
            dependency_swap["candidate"] = copy.deepcopy(
                browser["block_strategies"]
            )
            dependency_swap["attempted"] = True
            legacy._rebuild_strategy_dependencies(
                current_app,
                browser["block_strategies"],
            )
            dependency_swap["applied"] = True
            return browser

        browser = legacy.mutate_persisted_strategy_state(update)
        current_app.config[
            "SELECTOR_PROBE_DEPENDENCY_SYNC_FAILED"
        ].difference_update(
            legacy._strategy_ids(dependency_swap["previous"])
            | legacy._strategy_ids(dependency_swap["candidate"])
        )
        return jsonify({"strategies": browser["block_strategies"]})
    except (TypeError, ValueError) as error:
        if dependency_swap["attempted"]:
            legacy._rollback_strategy_dependencies(
                current_app,
                dependency_swap["previous"],
                dependency_swap["candidate"],
            )
            return jsonify(
                {"error": "dependency_index_unavailable"}
            ), 503
        return jsonify({"error": str(error)}), 400
    except Exception:
        if dependency_swap["attempted"]:
            legacy._rollback_strategy_dependencies(
                current_app,
                dependency_swap["previous"],
                dependency_swap["candidate"],
            )
        return jsonify(
            {
                "error": (
                    "strategy_save_unavailable"
                    if dependency_swap["applied"]
                    else "dependency_index_unavailable"
                )
            }
        ), 503

@bp.route("/api/browser/action-config", methods=["GET", "PUT"])
@bp.route("/api/browser/auto-strategies", methods=["GET", "PUT"])
@bp.post("/api/browser/auto-strategies/generate")
def retired_legacy_strategy_api():
    return jsonify({
        "error": (
            "旧策略接口已停用；请使用 /api/browser/elements 管理元素，"
            "并使用 /api/browser/strategies 管理统一积木策略"
        )
    }), 410

@bp.post("/api/browser/batch-tasks")
def start_browser_batch_task_route():
    from browser_strategy_runtime import build_batches

    payload = request.get_json(silent=True) or {}
    strategy_id = str(payload.get("strategy_id") or "")
    try:
        browser = legacy.load_persisted_strategy_state()
        strategy = next(
            item
            for item in browser["block_strategies"]
            if item.get("id") == strategy_id
        )
        if strategy.get("status") == "needs_repair":
            detail = "; ".join(strategy.get("repair_errors") or [])
            raise ValueError(f"strategy needs repair before execution: {detail}")
    except StopIteration:
        return jsonify({"error": "执行策略不存在"}), 404
    except (TypeError, ValueError) as error:
        return jsonify({"error": str(error)}), 400
    initial_gate = legacy.browser_strategy_gate_check(current_app, strategy["id"])
    if initial_gate.get("allowed") is not True:
        return jsonify(
            {
                "error": "strategy_paused",
                "code": "strategy_paused",
                "strategy_id": legacy._public_identifier(
                    strategy["id"],
                    "strategy",
                ),
                "reasons": initial_gate.get("reasons", []),
            }
        ), 409
    try:
        batch_size = int(payload.get("batch_size", strategy.get("batch_size", 4)))
        build_batches([], batch_size)
    except (TypeError, ValueError) as error:
        return jsonify({"error": str(error)}), 400

    profiles = payload.get("windows")
    if not profiles:
        try:
            profiles = legacy.fetch_adspower_windows().get("windows", [])
        except (requests.RequestException, RuntimeError) as error:
            return jsonify({"error": str(error)}), 502
    if not isinstance(profiles, list) or not profiles:
        return jsonify({"error": "没有读取到可执行的 AdsPower 窗口"}), 400
    normalized_profiles = []
    for item in profiles:
        if isinstance(item, str):
            item = {"profile_id": item}
        if not isinstance(item, dict) or not str(item.get("profile_id") or "").strip():
            return jsonify({"error": "窗口必须包含 profile_id"}), 400
        normalized_profiles.append({**item, "profile_id": str(item["profile_id"]).strip()})

    target_url = str(
        payload.get("url")
        or load_settings().get("browser", {}).get("default_url")
        or "https://www.tiktok.com/"
    ).strip()
    if not legacy.is_valid_browser_url(target_url):
        return jsonify({"error": "url must be a valid http:// or https:// URL"}), 400

    from browser_strategy_runtime import build_batches

    batches = build_batches(normalized_profiles, batch_size)
    task_id = f"browser-batch-{uuid4().hex[:12]}"
    task = {
        "id": task_id,
        "status": "queued",
        "strategy_id": strategy_id,
        "batch_size": batch_size,
        "total_windows": len(normalized_profiles),
        "total_batches": len(batches),
        "completed_batches": 0,
        "processed_windows": 0,
        "failed_windows": 0,
        "created_at": content_now_iso(),
        "results": [],
    }
    with BROWSER_BATCH_TASKS_LOCK:
        BROWSER_BATCH_TASKS[task_id] = task
    threading.Thread(
        target=run_browser_batch_task,
        args=(current_app, task_id, normalized_profiles, batch_size, strategy, target_url),
        name=f"browser-batch-{task_id}",
        daemon=True,
    ).start()
    public_task = legacy.public_browser_batch_task(task)
    legacy.record_browser_log("batch_task_created", public_task)
    return jsonify(public_task), 202

@bp.get("/api/browser/batch-tasks")
def list_browser_batch_tasks_route():
    with BROWSER_BATCH_TASKS_LOCK:
        tasks = [dict(item) for item in BROWSER_BATCH_TASKS.values()]
    tasks.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    return jsonify(
        {
            "count": len(tasks),
            "tasks": [legacy.public_browser_batch_task(item) for item in tasks],
        }
    )

@bp.get("/api/browser/batch-tasks/<task_id>")
def get_browser_batch_task_route(task_id):
    with BROWSER_BATCH_TASKS_LOCK:
        task = BROWSER_BATCH_TASKS.get(task_id)
        if task is not None:
            return jsonify(legacy.public_browser_batch_task(task))
    return jsonify({"error": "批量任务不存在"}), 404

@bp.post("/api/browser/direct-agent")
def start_direct_agent_route():
    payload = request.get_json(silent=True) or {}
    try:
        command = legacy.build_direct_agent_command(payload)
    except ValueError as error:
        return jsonify({"error": str(error)}), 400

    process = subprocess.Popen(
        command,
        cwd=PROJECT_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=False,
    )
    return jsonify(
        {
            "status": "started",
            "pid": process.pid,
            "command": command,
        }
    ), 202

@bp.get("/api/browser/adspower-windows")
def adspower_windows_route():
    try:
        return jsonify(legacy.fetch_adspower_windows())
    except requests.exceptions.RequestException as error:
        return jsonify({"error": str(error), "count": 0, "windows": []}), 502
    except RuntimeError as error:
        return jsonify({"error": str(error), "count": 0, "windows": []}), 502

@bp.get("/api/browser/logs")
def browser_logs_route():
    try:
        limit = min(max(int(request.args.get("limit", 100)), 1), 500)
    except (TypeError, ValueError):
        limit = 100
    if not legacy.BROWSER_LOG_PATH.exists():
        return jsonify({"count": 0, "logs": [], "path": str(legacy.BROWSER_LOG_PATH)})
    try:
        lines = legacy.BROWSER_LOG_PATH.read_text(encoding="utf-8").splitlines()[-limit:]
        logs = [json.loads(line) for line in lines if line.strip()]
    except (OSError, json.JSONDecodeError) as error:
        return jsonify({"error": str(error), "count": 0, "logs": []}), 500
    logs = legacy.public_browser_payload(logs)
    return jsonify({"count": len(logs), "logs": logs, "path": str(legacy.BROWSER_LOG_PATH)})

@bp.get("/api/browser/sessions")
def browser_sessions_route():
    with ACTIVE_BROWSER_SESSIONS_LOCK:
        sessions = [
            {
                "profile_id": profile_id,
                "status": "active",
            }
            for profile_id, ws_url in ACTIVE_BROWSER_SESSIONS.items()
            if ws_url
        ]
    return jsonify({"count": len(sessions), "sessions": sessions})

@bp.post("/api/browser/open-tile")
def open_and_tile_browsers_route():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "请求格式无效，必须是 JSON 对象"}), 400
    try:
        profiles = legacy.normalize_selected_browser_profiles(payload.get("windows"))
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    target_url = legacy.get_browser_target_url(payload)
    if not legacy.is_valid_browser_url(target_url):
        return jsonify({"error": "url must be a valid http:// or https:// URL"}), 400

    session_results, layout = legacy.ensure_browser_profile_sessions(
        profiles, lease_sessions=True
    )
    successful = [item for item in session_results if item.get("status") == "ready"]
    ready_profile_ids = [item["profile_id"] for item in successful]
    tile_errors = {
        profile_id: legacy.browser_tile_error(layout, profile_id, ready_profile_ids)
        for profile_id in ready_profile_ids
    }
    results = [
        {
            "profile_id": item["profile_id"],
            "profile_no": item.get("profile_no", ""),
            "name": item.get("name", ""),
            "status": (
                "started"
                if item.get("status") == "ready"
                and not tile_errors.get(item["profile_id"], "")
                else "failed"
            ),
            "stage": (
                "tile"
                if item.get("status") == "ready"
                and tile_errors.get(item["profile_id"], "")
                else item["stage"]
            ),
            "attempts": item["attempts"],
            "target_url": target_url,
            "error": (
                tile_errors.get(item["profile_id"], "")
                if item.get("status") == "ready"
                else item.get("error", "")
            ),
        }
        for item in session_results
    ]

    def prepare_one(item):
        failure = None
        for attempt in range(1, 4):
            try:
                prepared = legacy.prepare_browser_page(item["ws_url"], target_url)
                return {
                    "profile_id": item["profile_id"],
                    "status": "ok",
                    "stage": "navigate",
                    "attempts": attempt,
                    "target_url": target_url,
                    "url": target_url,
                    "current_url": prepared["current_url"],
                    "closed_tabs": prepared["closed_tabs"],
                }
            except BrowserStageError as error:
                failure = {
                    "profile_id": item["profile_id"],
                    "status": "failed",
                    "stage": error.stage,
                    "attempts": attempt,
                    "target_url": error.target_url,
                    "error": error.reason,
                }
            except Exception as error:
                failure = {
                    "profile_id": item["profile_id"],
                    "status": "failed",
                    "stage": "navigate",
                    "attempts": attempt,
                    "target_url": target_url,
                    "error": str(error),
                }
            if attempt < 3:
                time.sleep(2)
        return failure

    navigation = []
    tiled = [
        item
        for item in successful
        if not tile_errors.get(item["profile_id"], "")
    ]
    try:
        if tiled:
            with ThreadPoolExecutor(max_workers=len(tiled)) as executor:
                navigation = list(executor.map(prepare_one, tiled))
    finally:
        legacy.release_browser_session_results(session_results)

    response_payload = legacy.public_browser_payload({
        "task_id": uuid4().hex,
        "requested": len(profiles),
        "started": len(successful),
        "failed": len(profiles) - len(successful),
        "url": target_url,
        "results": results,
        "layout": layout,
        "navigation": navigation,
    })
    legacy.record_browser_log("open_tile", response_payload)
    return jsonify(response_payload)

@bp.post("/api/browser/search-agent")
def start_search_agent_route():
    payload = request.get_json(silent=True) or {}
    try:
        command = legacy.build_search_agent_command(payload)
    except ValueError as error:
        return jsonify({"error": str(error)}), 400

    process = subprocess.Popen(
        command,
        cwd=PROJECT_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=False,
    )
    return jsonify(
        {
            "status": "started",
            "pid": process.pid,
            "command": command,
        }
    ), 202

@bp.get("/api/execution-strategies")
def get_execution_strategies_route():
    return jsonify(load_settings().get("execution_strategies", {"items": []}))

@bp.route("/api/execution-strategies", methods=["PUT", "POST"])
def save_execution_strategies_route():
    payload = request.get_json(silent=True) or {}
    try:
        return jsonify(legacy.save_execution_strategies(payload))
    except (TypeError, ValueError) as error:
        return jsonify({"error": str(error)}), 400

@bp.post("/api/execution-strategies/generate")
def generate_execution_strategies_route():
    payload = request.get_json(silent=True) or {}
    try:
        return jsonify(legacy.generate_execution_strategies(payload.get("prompt", "")))
    except (json.JSONDecodeError, requests.exceptions.RequestException, ValueError) as error:
        return jsonify({"error": str(error)}), 400
