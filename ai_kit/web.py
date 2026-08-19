"""Loopback-only FastAPI interface for AI Kit."""

import hashlib
import json
import logging
import os
import secrets
import threading
import time
import webbrowser
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .core import (
    AiKitError,
    OperationEvent,
    OperationRequest,
    catalog_hosts,
    git_storage_revision,
    load_config,
    load_manifest,
    operation_guard,
    redacted_git_url,
    run_operation,
    storage_lock_identities,
    storage_roots,
)


PACKAGE_ROOT = Path(__file__).resolve().parent
PREVIEW_TTL_SECONDS = 10 * 60
LOGGER = logging.getLogger(__name__)


@dataclass
class StoredPreview:
    request: OperationRequest
    created_at: float
    plan: str


@dataclass
class OperationJob:
    job_id: str
    request: OperationRequest
    expected_plan: str
    events: list[dict] = field(default_factory=list)
    done: bool = False
    condition: threading.Condition = field(default_factory=threading.Condition)
    thread: threading.Thread | None = field(default=None, repr=False)

    def publish(self, payload: dict) -> None:
        with self.condition:
            self.events.append(payload)
            self.condition.notify_all()

    def finish(self, success: bool, message: str) -> None:
        with self.condition:
            self.events.append(
                {
                    "kind": "complete" if success else "error",
                    "message": message,
                    "tone": "success" if success else "danger",
                    "done": True,
                    "success": success,
                }
            )
            self.done = True
            self.condition.notify_all()


def event_payload(event: OperationEvent) -> dict:
    if event.kind in ("backup", "restore"):
        tone = "change"
    elif event.kind in ("prune", "conflict", "different", "unbacked"):
        tone = "danger"
    elif event.kind in ("keep", "no-sources", "catalog-only"):
        tone = "warning"
    elif event.kind == "clean":
        tone = "success"
    else:
        tone = "quiet"
    return {
        "kind": event.kind,
        "message": event.message,
        "tool": event.tool,
        "artifact": event.artifact,
        "tone": tone,
        "done": False,
    }


def update_tree_fingerprint(digest: object, path: Path) -> None:
    digest.update(str(path).encode("utf-8"))
    digest.update(b"\0")
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        digest.update(b"missing\0")
        return
    except OSError as exc:
        raise AiKitError("Cannot inspect {}: {}".format(path, exc))

    digest.update(str(metadata.st_mode).encode("ascii"))
    digest.update(b"\0")
    try:
        if path.is_symlink():
            digest.update(b"symlink\0")
            digest.update(os.readlink(str(path)).encode("utf-8"))
        elif path.is_file():
            digest.update(b"file\0")
            with path.open("rb") as source:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
        elif path.is_dir():
            digest.update(b"directory\0")
            for child in sorted(path.iterdir(), key=lambda item: item.name):
                update_tree_fingerprint(digest, child)
        else:
            digest.update(b"special\0")
    except OSError as exc:
        raise AiKitError("Cannot inspect {}: {}".format(path, exc))
    digest.update(b"\0")


def plan_signature(changes: int, events: list[dict], filesystem: str) -> str:
    reviewed = {
        "changes": changes,
        "filesystem": filesystem,
        "events": [
            {
                "kind": event["kind"],
                "message": event["message"],
                "tool": event["tool"],
                "artifact": event["artifact"],
            }
            for event in events
        ],
    }
    serialized = json.dumps(reviewed, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class WebRuntime:
    def __init__(self, config_path: Path, host_override: str | None) -> None:
        self.config_path = config_path.expanduser().resolve()
        self.host_override = host_override
        self.csrf_token = secrets.token_urlsafe(32)
        self.previews: dict[str, StoredPreview] = {}
        self.jobs: dict[str, OperationJob] = {}
        self.state_lock = threading.Lock()
        self.operation_lock = threading.Lock()

    def available_hosts(self) -> tuple[dict, list[str]]:
        config = load_config(self.config_path, self.host_override)
        hosts = set(catalog_hosts(config))
        hosts.add(config["_host"])
        return config, sorted(hosts)

    @contextmanager
    def operation_guard(self, host: str | None = None):
        config = load_config(self.config_path, host or self.host_override)
        locked_identities = storage_lock_identities(config)
        with operation_guard(self.config_path, *locked_identities):
            current = load_config(self.config_path, host or self.host_override)
            if {
                item.expanduser().resolve() for item in storage_lock_identities(current)
            } != {item.expanduser().resolve() for item in locked_identities}:
                raise AiKitError("Storage configuration changed while waiting; retry the operation")
            yield

    def validate_host(self, host: str) -> str:
        config, hosts = self.available_hosts()
        selected = host or config["_host"]
        if selected not in hosts:
            raise AiKitError("Unknown catalog host: {}".format(selected))
        return selected

    def filesystem_signature(self, request: OperationRequest) -> str:
        config = load_config(self.config_path, request.host)
        roots = {
            self.config_path,
            config["_state_file"],
        }
        roots.update(storage_roots(config))
        for tool in config["tools"].values():
            for section_name in ("skills", "commands"):
                section = tool[section_name]
                roots.add(section["_target"])
                roots.update(source["path"] for source in section["_sources"])
        digest = hashlib.sha256()
        revision = git_storage_revision(config)
        digest.update((revision or "no-git-revision").encode("ascii"))
        digest.update(b"\0")
        for root in sorted(roots, key=lambda item: str(item)):
            update_tree_fingerprint(digest, root)
        return digest.hexdigest()

    def preview(self, request: OperationRequest) -> tuple[str, int, list[dict]]:
        events: list[dict] = []
        preview_request = replace(request, dry_run=True)
        with self.operation_lock:
            with self.operation_guard(request.host):
                filesystem_before = None

                def capture_filesystem() -> None:
                    nonlocal filesystem_before
                    filesystem_before = self.filesystem_signature(request)

                changes = run_operation(
                    self.config_path,
                    preview_request,
                    lambda event: events.append(event_payload(event)),
                    acquire_lock=False,
                    pre_plan=capture_filesystem,
                )
                filesystem = self.filesystem_signature(request)
                if filesystem_before is None:
                    raise AiKitError("The operation did not prepare a filesystem preview")
                if filesystem_before != filesystem:
                    raise AiKitError(
                        "The filesystem changed while building this preview; try again"
                    )
        token = secrets.token_urlsafe(24)
        now = time.monotonic()
        with self.state_lock:
            self.previews = {
                key: value
                for key, value in self.previews.items()
                if now - value.created_at <= PREVIEW_TTL_SECONDS
            }
            self.previews[token] = StoredPreview(
                replace(request, dry_run=False),
                now,
                plan_signature(changes, events, filesystem),
            )
        return token, changes, events

    def consume_preview(self, token: str) -> StoredPreview:
        with self.state_lock:
            preview = self.previews.pop(token, None)
        if preview is None or time.monotonic() - preview.created_at > PREVIEW_TTL_SECONDS:
            raise AiKitError("This preview expired or was already used; preview the operation again")
        return preview

    def start_job(self, preview: StoredPreview) -> OperationJob:
        with self.state_lock:
            if any(not existing.done for existing in self.jobs.values()):
                raise AiKitError("Another operation is still running; wait for it to finish")
            if len(self.jobs) >= 50:
                completed = [key for key, value in self.jobs.items() if value.done]
                for key in completed[:25]:
                    self.jobs.pop(key, None)
            job = OperationJob(
                secrets.token_urlsafe(18), preview.request, preview.plan
            )
            self.jobs[job.job_id] = job
        thread = threading.Thread(target=self._run_job, args=(job,), daemon=False)
        job.thread = thread
        thread.start()
        return job

    def _run_job(self, job: OperationJob) -> None:
        try:
            with self.operation_lock:
                with self.operation_guard(job.request.host):
                    current_events: list[dict] = []
                    filesystem_before = None

                    def capture_filesystem() -> None:
                        nonlocal filesystem_before
                        filesystem_before = self.filesystem_signature(job.request)

                    current_changes = run_operation(
                        self.config_path,
                        replace(job.request, dry_run=True),
                        lambda event: current_events.append(event_payload(event)),
                        acquire_lock=False,
                        pre_plan=capture_filesystem,
                    )
                    filesystem = self.filesystem_signature(job.request)
                    if filesystem_before is None:
                        raise AiKitError("The operation did not prepare a filesystem confirmation")
                    if filesystem_before != filesystem:
                        raise AiKitError(
                            "The filesystem changed during confirmation; review a new dry run"
                        )
                    if (
                        plan_signature(current_changes, current_events, filesystem)
                        != job.expected_plan
                    ):
                        raise AiKitError(
                            "The filesystem changed after this preview; review a new dry run"
                        )

                    def verify_reviewed_filesystem() -> None:
                        latest = self.filesystem_signature(job.request)
                        if (
                            plan_signature(current_changes, current_events, latest)
                            != job.expected_plan
                        ):
                            raise AiKitError(
                                "The filesystem changed before execution; review a new dry run"
                            )

                    changes = run_operation(
                        self.config_path,
                        job.request,
                        lambda event: job.publish(event_payload(event)),
                        acquire_lock=False,
                        pre_apply=verify_reviewed_filesystem,
                    )
            noun = "change" if changes == 1 else "changes"
            job.finish(True, "Operation complete: {} {} applied.".format(changes, noun))
        except AiKitError as exc:
            job.finish(False, "Operation stopped: {}".format(exc))
        except Exception:
            LOGGER.exception("Unexpected UI operation failure")
            job.finish(False, "Operation stopped because of an unexpected internal error.")

    def wait_for_jobs(self) -> None:
        with self.state_lock:
            threads = [job.thread for job in self.jobs.values() if job.thread is not None]
        for thread in threads:
            thread.join()

    def get_job(self, job_id: str) -> OperationJob:
        with self.state_lock:
            job = self.jobs.get(job_id)
        if job is None:
            raise AiKitError("Unknown operation log")
        return job

    def dashboard(self, selected_host: str) -> dict:
        events: list[OperationEvent] = []
        with self.operation_lock:
            with self.operation_guard(selected_host):
                run_operation(
                    self.config_path,
                    OperationRequest("status", host=selected_host),
                    events.append,
                    acquire_lock=False,
                )
                config = load_config(self.config_path, selected_host)
                tools = []
                inventory = []
                for tool_name in sorted(config["tools"]):
                    manifest = load_manifest(config, tool_name)
                    tool_events = [event for event in events if event.tool == tool_name]
                    kinds = {event.kind for event in tool_events}
                    if kinds.intersection({"unbacked", "different", "conflict"}):
                        state = "attention"
                        state_label = "Needs attention"
                    elif "no-sources" in kinds:
                        state = "offline"
                        state_label = "Sources unavailable"
                    else:
                        state = "clean"
                        state_label = "In sync"

                    sources = []
                    for section_name in ("skills", "commands"):
                        for source in config["tools"][tool_name][section_name]["_sources"]:
                            sources.append(
                                {
                                    "id": source["id"],
                                    "kind": section_name,
                                    "path": str(source["path"]),
                                    "available": source["path"].is_dir()
                                    and not source["path"].is_symlink(),
                                }
                            )
                    tools.append(
                        {
                            "name": tool_name,
                            "state": state,
                            "state_label": state_label,
                            "artifact_count": len(manifest["artifacts"]),
                            "sources": sources,
                            "events": [event_payload(event) for event in tool_events],
                        }
                    )
                    for artifact in manifest["artifacts"]:
                        inventory.append(
                            {
                                "tool": tool_name,
                                "kind": artifact["kind"],
                                "name": artifact["name"],
                                "path": artifact["path"],
                                "sources": ", ".join(
                                    source["id"] for source in artifact.get("sources", [])
                                )
                                or "catalog",
                            }
                        )
        return {"tools": tools, "inventory": inventory, "config": config}


def checked_csrf(runtime: WebRuntime, supplied: object) -> None:
    value = str(supplied or "")
    if not secrets.compare_digest(value, runtime.csrf_token):
        raise HTTPException(status_code=403, detail="Invalid request token")


def checked_port(value: int) -> int:
    if value < 1 or value > 65535:
        raise AiKitError("Port must be between 1 and 65535")
    return value


def form_flag(form: object, name: str) -> bool:
    return str(form.get(name, "")).lower() in ("1", "true", "yes", "on")


def operation_from_form(runtime: WebRuntime, form: object) -> OperationRequest:
    action = str(form.get("action", ""))
    if action == "storage":
        config = load_config(runtime.config_path, runtime.host_override)
        return OperationRequest(
            "storage",
            host=config["_host"],
            storage_local=(
                str(form.get("storage_local", "")).strip()
                if form_flag(form, "local_enabled")
                else None
            ),
            storage_git=(
                str(form.get("storage_git", "")).strip()
                if form_flag(form, "git_enabled")
                else None
            ),
        )
    if action not in ("backup", "restore"):
        raise AiKitError("Choose a backup, restore, or storage operation")
    tool = str(form.get("tool", "all"))
    selected_host = runtime.validate_host(str(form.get("host", "")))
    if action == "backup":
        return OperationRequest(
            "backup",
            tool=tool,
            host=selected_host,
            prune=form_flag(form, "prune"),
            include_derived=form_flag(form, "include_derived"),
        )

    source_mode = str(form.get("source_mode", "matching"))
    source_tool = None
    all_tools = False
    if source_mode == "all":
        all_tools = True
    elif source_mode.startswith("tool:"):
        source_tool = source_mode.split(":", 1)[1]
    elif source_mode != "matching":
        raise AiKitError("Choose a valid restore source")
    as_backed_up = str(form.get("restore_mode", "portable")) == "exact"
    return OperationRequest(
        "restore",
        tool=tool,
        host=selected_host,
        source_tool=source_tool,
        all_tools=all_tools,
        all_hosts=form_flag(form, "all_hosts"),
        as_backed_up=as_backed_up,
        force=form_flag(form, "force"),
    )


def operation_label(request: OperationRequest) -> str:
    if request.action == "storage":
        return "Update storage configuration"
    target = "every configured tool" if request.tool == "all" else request.tool
    if request.action == "backup":
        return "Back up {}".format(target)
    mode = "exact originals" if request.as_backed_up else "portable skills"
    return "Restore {} to {}".format(mode, target)


def create_app(config_path: Path, host_override: str | None = None) -> FastAPI:
    runtime = WebRuntime(config_path, host_override)
    initial_config = load_config(runtime.config_path, host_override)
    templates = Jinja2Templates(directory=str(PACKAGE_ROOT / "templates"))
    app = FastAPI(title="AI Kit", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.runtime = runtime
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost", "testserver"],
    )
    app.mount("/static", StaticFiles(directory=str(PACKAGE_ROOT / "static")), name="static")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; "
            "style-src 'self'; connect-src 'self'; img-src 'self' data:; "
            "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    def base_context(request: Request, selected_host: str) -> dict:
        config, hosts = runtime.available_hosts()
        local_root = config["_storage_local"]
        git_url = config["_storage_git_url"]
        form_git_url = redacted_git_url(git_url) if git_url is not None else ""
        if git_url is not None and form_git_url != git_url:
            form_git_url = ""
        if local_root is not None and git_url is not None:
            storage_label = "Local + Git"
        elif git_url is not None:
            storage_label = "Git"
        else:
            storage_label = "Local"
        return {
            "request": request,
            "csrf_token": runtime.csrf_token,
            "config_path": str(runtime.config_path),
            "catalog_path": str(config["_catalog_root"]),
            "storage_label": storage_label,
            "local_storage_path": str(local_root) if local_root is not None else None,
            "git_storage_url": redacted_git_url(git_url) if git_url is not None else None,
            "git_storage_form_url": form_git_url,
            "state_path": str(config["_state_file"]),
            "safety_path": str(config["_safety_backups"]),
            "tool_names": sorted(config["tools"]),
            "hosts": hosts,
            "selected_host": selected_host,
        }

    async def dashboard_context(request: Request, host: str | None) -> dict:
        selected_host = runtime.validate_host(host or initial_config["_host"])
        dashboard = await run_in_threadpool(runtime.dashboard, selected_host)
        context = base_context(request, selected_host)
        context.update(dashboard)
        return context

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request, host: str | None = None):
        context = await dashboard_context(request, host)
        return templates.TemplateResponse(request=request, name="index.html", context=context)

    @app.get("/dashboard", response_class=HTMLResponse)
    async def dashboard(request: Request, host: str | None = None):
        context = await dashboard_context(request, host)
        return templates.TemplateResponse(
            request=request, name="partials/dashboard.html", context=context
        )

    @app.post("/operations/preview", response_class=HTMLResponse)
    async def preview_operation(request: Request):
        form = await request.form()
        checked_csrf(runtime, form.get("csrf_token"))
        context = {"request": request, "csrf_token": runtime.csrf_token}
        try:
            operation = operation_from_form(runtime, form)
            token, changes, events = await run_in_threadpool(runtime.preview, operation)
            context.update(
                {
                    "preview_token": token,
                    "operation": operation,
                    "operation_label": operation_label(operation),
                    "changes": changes,
                    "events": events,
                    "dangerous": operation.prune
                    or operation.force
                    or operation.action in ("restore", "storage"),
                }
            )
            return templates.TemplateResponse(
                request=request, name="partials/preview.html", context=context
            )
        except AiKitError as exc:
            context["error"] = str(exc)
            return templates.TemplateResponse(
                request=request,
                name="partials/error.html",
                context=context,
                status_code=422,
            )

    @app.post("/operations/execute", response_class=HTMLResponse)
    async def execute_operation(request: Request):
        form = await request.form()
        checked_csrf(runtime, form.get("csrf_token"))
        try:
            preview = runtime.consume_preview(str(form.get("preview_token", "")))
            job = runtime.start_job(preview)
        except AiKitError as exc:
            return templates.TemplateResponse(
                request=request,
                name="partials/error.html",
                context={"request": request, "error": str(exc)},
                status_code=409,
            )
        return templates.TemplateResponse(
            request=request,
            name="partials/running.html",
            context={
                "request": request,
                "job_id": job.job_id,
                "csrf_token": runtime.csrf_token,
                "operation_label": operation_label(preview.request),
                "selected_host": preview.request.host,
                "reload_page": preview.request.action == "storage",
            },
        )

    @app.get("/operations/{job_id}/events")
    async def operation_events(request: Request, job_id: str, token: str):
        checked_csrf(runtime, token)
        try:
            job = runtime.get_job(job_id)
        except AiKitError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

        try:
            initial_index = max(0, int(request.headers.get("last-event-id", "0")))
        except ValueError:
            initial_index = 0

        def stream():
            index = initial_index
            while True:
                with job.condition:
                    if index >= len(job.events) and not job.done:
                        job.condition.wait(timeout=15)
                    pending = job.events[index:]
                    finished = job.done
                if not pending:
                    if finished:
                        return
                    yield ": keepalive\n\n"
                    continue
                for payload in pending:
                    index += 1
                    yield "id: {}\ndata: {}\n\n".format(
                        index, json.dumps(payload)
                    )
                if finished:
                    return

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no"},
        )

    return app


def run_browser(
    config_path: Path,
    host_override: str | None = None,
    bind: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
) -> int:
    import uvicorn

    checked_port(port)
    if bind not in ("127.0.0.1", "localhost"):
        raise AiKitError("The UI can only bind to the local loopback interface")
    app = create_app(config_path, host_override)
    url = "http://{}:{}/".format(bind, port)
    if open_browser:
        timer = threading.Timer(0.6, lambda: webbrowser.open(url))
        timer.daemon = True
        timer.start()
    try:
        uvicorn.run(app, host=bind, port=port, log_level="warning")
    finally:
        app.state.runtime.wait_for_jobs()
    return 0
