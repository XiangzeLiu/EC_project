"""Trader-facing software center routes."""

from __future__ import annotations

import os

from fastapi import APIRouter, Form, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import database
from services import software_access, software_release_service

router = APIRouter(tags=["Software Center"])
_TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")
templates = Jinja2Templates(directory=_TEMPLATES_DIR)


def _login_error(request: Request, message: str):
    return templates.TemplateResponse(request, "software_login.html", {"error": message})


def _trader_release_view(release: dict) -> dict:
    item = {key: value for key, value in release.items() if key != "artifacts"}
    item["artifacts"] = [
        {key: value for key, value in artifact.items() if key != "storage_key"}
        for artifact in release.get("artifacts") or []
    ]
    return item


@router.get("/download")
@router.get("/download/")
async def trader_download_entry(request: Request):
    if software_access.get_trader_session(request):
        return RedirectResponse("/software/trader", status_code=302)
    return RedirectResponse("/software/login", status_code=302)


@router.get("/software/login")
async def software_login_page(request: Request):
    if software_access.get_trader_session(request):
        return RedirectResponse("/software/trader", status_code=302)
    return templates.TemplateResponse(request, "software_login.html", {"error": ""})


@router.post("/software/login")
async def software_login_submit(request: Request, username: str = Form(""), password: str = Form("")):
    username = str(username or "").strip()
    password = str(password or "")
    account = database.get_account_by_username(username)
    verified = database.verify_account(username, password) if username and password else None
    if not verified or verified.get("role") != "trader" or verified.get("status") != "active":
        return _login_error(request, "交易员账号或密码错误")
    sid = software_access.create_trader_session(account or verified)
    response = RedirectResponse("/software/trader", status_code=302)
    software_access.set_session_cookie(response, sid)
    database.record_audit_log(username, "SOFTWARE_LOGIN", "software_center", "Trader software center login", request.client.host if request.client else "")
    return response


@router.get("/software/trader")
async def software_trader_page(request: Request):
    session = software_access.get_trader_session(request)
    if not session:
        return RedirectResponse("/software/login", status_code=302)
    releases = [
        _trader_release_view(item)
        for item in database.list_software_releases("client", trader_visible_only=True)
    ]
    return templates.TemplateResponse(
        request,
        "software_portal.html",
        {"username": session.get("username") or "", "releases": releases},
    )


@router.get("/software/logout")
async def software_logout(request: Request):
    software_access.invalidate_session(request)
    response = RedirectResponse("/software/login", status_code=302)
    software_access.clear_session_cookie(response)
    return response


@router.get("/software/releases/{release_id}/download")
async def trader_software_download(request: Request, release_id: str):
    session = software_access.get_trader_session(request)
    if not session:
        return RedirectResponse("/software/login", status_code=302)
    release = database.get_software_release(release_id)
    if not release or release.get("product_type") != "client" or release.get("status") != "published" or not release.get("trader_visible"):
        return RedirectResponse("/software/trader?error=unavailable", status_code=303)
    try:
        release, artifact, path = software_release_service.resolve_artifact(release_id)
    except FileNotFoundError:
        return RedirectResponse("/software/trader?error=unavailable", status_code=303)
    database.record_audit_log(
        str(session.get("username") or ""),
        "SOFTWARE_DOWNLOAD",
        "software_release",
        f"Downloaded client release {release_id}",
        request.client.host if request.client else "",
    )
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=str(artifact.get("file_name") or "SC_Client.exe"),
        headers={"Cache-Control": "private, no-store", "X-SHA256": str(artifact.get("sha256") or "")},
    )
