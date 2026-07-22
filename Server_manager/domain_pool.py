"""TS domain-pool validation, DNS updates, allocation, and release flow."""

from __future__ import annotations

import ipaddress
import logging
from datetime import datetime, timezone

import database
from config import (
    SM_DOMAIN_COOLDOWN_SECONDS,
    SM_DOMAIN_ROOT,
    SM_TS_WS_PATH,
)
from dnspod_client import DNSPodClient, DNSPodError

log = logging.getLogger("server_manager.domain_pool")


class DomainPoolError(RuntimeError):
    pass


def normalize_public_ipv4(value: str) -> str:
    raw = (value or "").strip()
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError as exc:
        raise DomainPoolError("TS public_ip must be a valid IPv4 address") from exc
    if not isinstance(ip, ipaddress.IPv4Address):
        raise DomainPoolError("TS public_ip must be IPv4 for DNS A records")
    if not ip.is_global:
        raise DomainPoolError("TS public_ip must be a public IPv4 address")
    return str(ip)


def normalize_domain(fqdn: str) -> dict:
    domain = (fqdn or "").strip().lower().strip(".")
    root = SM_DOMAIN_ROOT.strip().lower().strip(".")
    if not domain or len(domain) > 253:
        raise DomainPoolError("invalid domain name")
    if domain == root or not domain.endswith(f".{root}"):
        raise DomainPoolError(f"TS domain must be a subdomain of {root}")
    labels = domain.split(".")
    for label in labels:
        if not label or len(label) > 63 or label.startswith("-") or label.endswith("-"):
            raise DomainPoolError(f"invalid domain label: {label or '-'}")
        if not label.isascii() or not all(ch.isalnum() or ch == "-" for ch in label):
            raise DomainPoolError(f"invalid domain label: {label}")
    record_suffix = f".{root}"
    if not domain.endswith(record_suffix):
        raise DomainPoolError(f"domain must be inside {root}")
    return {
        "fqdn": domain,
        "root_domain": root,
        "record_name": domain[: -len(record_suffix)],
        "public_endpoint": f"wss://{domain}{SM_TS_WS_PATH}",
    }


def import_domains(domains: list[str]) -> dict:
    entries = []
    errors = []
    seen = set()
    for raw in domains:
        try:
            entry = normalize_domain(raw)
        except DomainPoolError as exc:
            errors.append({"domain": (raw or "").strip(), "error": str(exc)})
            continue
        if entry["fqdn"] in seen:
            continue
        seen.add(entry["fqdn"])
        entries.append(entry)
    result = database.import_ts_domain_pool(entries)
    result["errors"] = errors
    result["accepted"] = len(entries)
    return result


def allocate_domain(node_name: str, public_ip: str) -> dict:
    normalized_ip = normalize_public_ipv4(public_ip)
    dns = DNSPodClient()
    try:
        dns.ensure_ready()
    except DNSPodError as exc:
        raise DomainPoolError(str(exc)) from exc

    reserved = database.reserve_ts_domain(node_name=node_name, public_ip=normalized_ip)
    if not reserved:
        raise DomainPoolError("no available TS domain in the pool")

    domain_id = int(reserved["id"])
    try:
        dns_result = dns.upsert_a_record(
            reserved["fqdn"],
            normalized_ip,
            reserved.get("dns_record_id", ""),
        )
        dns_status = {
            "mock": "mock",
            "manual": "manual",
        }.get(dns_result.mode, "active")
        if not database.update_reserved_domain_dns(domain_id, dns_result.record_id, dns_status):
            try:
                dns.delete_a_record(reserved["fqdn"], dns_result.record_id)
                database.abort_reserved_domain(domain_id, "reservation lost", reusable=True)
            except Exception as cleanup_exc:
                database.mark_ts_domain_error(domain_id, f"reservation lost; cleanup failed: {cleanup_exc}")
            raise DomainPoolError("domain reservation was lost during DNS update")
        reserved.update({
            "assigned_ip": normalized_ip,
            "dns_record_id": dns_result.record_id,
            "dns_status": dns_status,
            "dns_action": dns_result.action,
        })
        return reserved
    except DomainPoolError:
        raise
    except Exception as exc:
        if dns.mode == "manual":
            database.abort_reserved_domain(domain_id, str(exc), reusable=True)
        else:
            database.mark_ts_domain_error(domain_id, str(exc))
        log.exception("DNSPod allocation failed for %s", reserved.get("fqdn"))
        raise DomainPoolError(f"DNSPod update failed: {exc}") from exc


def abort_allocation(assignment: dict, reason: str) -> None:
    if not assignment:
        return
    domain_id = int(assignment.get("id") or 0)
    if not domain_id:
        return
    dns = DNSPodClient()
    reusable = False
    try:
        dns.delete_a_record(
            assignment.get("fqdn", ""),
            assignment.get("dns_record_id", ""),
        )
        reusable = True
    except Exception as exc:
        reason = f"{reason}; DNS cleanup failed: {exc}"
    database.abort_reserved_domain(domain_id, reason, reusable=reusable)


def release_server_domain(server_id: str) -> dict:
    assigned = database.get_ts_domain_for_server(server_id)
    if not assigned:
        return {"ok": True, "released": False, "message": "node has no assigned domain"}
    dns = DNSPodClient()
    dns_status = "released"
    error = ""
    action = ""
    try:
        result = dns.delete_a_record(
            assigned["fqdn"],
            assigned.get("dns_record_id", ""),
        )
        action = result.action
        dns_status = {
            "mock": "mock-released",
            "manual": "manual-released",
        }.get(result.mode, "released")
    except Exception as exc:
        error = str(exc)
        dns_status = "error"
        log.exception("DNSPod release failed for %s", assigned.get("fqdn"))

    released = database.release_ts_domain_for_server(
        server_id=server_id,
        cooldown_seconds=SM_DOMAIN_COOLDOWN_SECONDS,
        dns_status=dns_status,
        dns_error=error,
    )
    return {
        "ok": not bool(error),
        "released": bool(released),
        "domain": assigned.get("fqdn", ""),
        "dns_action": action,
        "error": error,
        "status": (released or {}).get("status", ""),
    }


def refresh_domain_dns(domain_id: int) -> dict:
    entry = database.get_ts_domain_pool_entry(domain_id)
    if not entry:
        raise DomainPoolError("domain not found")
    if entry.get("status") not in {"occupied", "error"} or not entry.get("assigned_ip"):
        raise DomainPoolError("only assigned domains can refresh DNS")
    dns = DNSPodClient()
    try:
        result = dns.upsert_a_record(
            entry["fqdn"],
            normalize_public_ipv4(entry["assigned_ip"]),
            entry.get("dns_record_id", ""),
        )
    except Exception as exc:
        database.mark_ts_domain_error(domain_id, str(exc))
        raise DomainPoolError(f"DNSPod refresh failed: {exc}") from exc
    dns_status = {
        "mock": "mock",
        "manual": "manual",
    }.get(result.mode, "active")
    database.update_reserved_domain_dns(domain_id, result.record_id, dns_status)
    return {"ok": True, "record_id": result.record_id, "action": result.action}


def release_orphan_domain(domain_id: int) -> dict:
    entry = database.get_ts_domain_pool_entry(domain_id)
    if not entry:
        raise DomainPoolError("domain not found")
    if entry.get("assigned_server_id"):
        raise DomainPoolError("delete the assigned TS node to release this domain")
    if entry.get("status") == "cooling" and entry.get("cooldown_until"):
        try:
            cooldown_until = datetime.fromisoformat(entry["cooldown_until"])
            if cooldown_until.tzinfo is None:
                cooldown_until = cooldown_until.replace(tzinfo=timezone.utc)
        except ValueError:
            cooldown_until = datetime.max.replace(tzinfo=timezone.utc)
        if cooldown_until > datetime.now(timezone.utc):
            raise DomainPoolError("domain is still cooling and cannot be reused yet")
    dns = DNSPodClient()
    try:
        result = dns.delete_a_record(
            entry["fqdn"],
            entry.get("dns_record_id", ""),
        )
    except Exception as exc:
        database.mark_ts_domain_error(domain_id, str(exc))
        raise DomainPoolError(f"DNSPod release failed: {exc}") from exc
    if not database.reset_ts_domain_entry(domain_id):
        raise DomainPoolError("domain state changed before release")
    return {"ok": True, "action": result.action}
