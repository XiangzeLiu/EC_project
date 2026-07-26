"""Tencent Cloud DNSPod A-record operations used by the TS domain pool."""

from __future__ import annotations

import hashlib
import json
import socket
from dataclasses import dataclass


class DNSPodError(RuntimeError):
    pass


_NO_RECORDS_CODE = "ResourceNotFound.NoDataOfRecord"


def _sdk_error_code(exc: Exception) -> str:
    getter = getattr(exc, "get_code", None)
    if callable(getter):
        try:
            return str(getter() or "").strip()
        except Exception:
            pass
    return str(getattr(exc, "code", "") or "").strip()


def _sdk_error_detail(exc: Exception) -> str:
    code = _sdk_error_code(exc)
    hints = {
        "AuthFailure.SecretIdNotFound": "DNSPod authentication failed; check SecretId",
        "AuthFailure.SignatureFailure": "DNSPod authentication failed; check SecretKey and system time",
        "AuthFailure.UnauthorizedOperation": "DNSPod permission is insufficient for this operation",
        "UnauthorizedOperation": "DNSPod permission is insufficient for this operation",
        "ResourceNotFound.DomainNotExist": "DNSPod root domain does not exist in this account",
    }
    hint = hints.get(code, "")
    if not hint and code.startswith(("RequestLimitExceeded", "LimitExceeded", "Throttling")):
        hint = "DNSPod request was rate limited; retry later"
    if hint:
        return f"{code}: {hint}"
    return str(exc)


@dataclass(frozen=True)
class DNSRecordResult:
    record_id: str
    action: str
    mode: str


class DNSPodClient:
    def __init__(self, config=None) -> None:
        if config is None:
            from services.dns_config_service import get_runtime_config

            config = get_runtime_config()
        self.mode = str(config.mode)
        self.root_domain = str(config.root_domain)
        self.secret_id = str(config.secret_id)
        self.secret_key = str(config.secret_key)
        self.line = str(config.record_line)
        self.ttl = int(config.ttl)
        self._client = None
        self._models = None

    def ensure_ready(self) -> None:
        if self.mode in {"mock", "manual"}:
            return
        if self.mode == "disabled":
            raise DNSPodError("DNSPod is disabled")
        if not self.secret_id or not self.secret_key:
            raise DNSPodError("DNSPod SecretId/SecretKey is not configured")
        self._get_sdk()

    def test_connection(self) -> str:
        self.ensure_ready()
        if self.mode == "mock":
            return "DNSPod mock mode is ready"
        if self.mode == "manual":
            return "DNSPod manual mode is ready"

        client, models = self._get_sdk()
        req = models.DescribeRecordListRequest()
        req.from_json_string(json.dumps({
            "Domain": self.root_domain,
            "Limit": 1,
        }))
        try:
            client.DescribeRecordList(req)
        except Exception as exc:
            if _sdk_error_code(exc) == _NO_RECORDS_CODE:
                return f"DNSPod API is reachable for {self.root_domain}; no records exist"
            raise DNSPodError(
                f"DNSPod connection test failed: {_sdk_error_detail(exc)}"
            ) from exc
        return f"DNSPod API is reachable for {self.root_domain}"

    def test_permissions(
        self,
        test_fqdn: str,
        initial_value: str = "192.0.2.10",
        modified_value: str = "192.0.2.11",
    ) -> dict:
        """Probe required DNSPod permissions using one temporary A record."""
        self.ensure_ready()
        if self.mode != "real":
            raise DNSPodError("DNSPod permission test requires real mode")

        record_name = self.record_name_for(test_fqdn)
        client, models = self._get_sdk()
        steps: list[dict] = []
        record_id = ""
        create_attempted = False
        cleanup_ok = True
        cleanup_lookup_error = ""

        try:
            try:
                existing_id = self._find_a_record_id(record_name)
                if existing_id:
                    raise DNSPodError("temporary DNS test record already exists")
                steps.append({
                    "key": "describe",
                    "label": "查询权限",
                    "status": "passed",
                    "detail": "DescribeRecordList succeeded; temporary record does not exist",
                })
            except Exception as exc:
                steps.append({
                    "key": "describe",
                    "label": "查询权限",
                    "status": "failed",
                    "detail": _sdk_error_detail(exc),
                })

            if steps[-1]["status"] == "passed":
                try:
                    create_attempted = True
                    req = models.CreateRecordRequest()
                    req.from_json_string(json.dumps({
                        "Domain": self.root_domain,
                        "SubDomain": record_name,
                        "RecordType": "A",
                        "RecordLine": self.line,
                        "Value": initial_value,
                        "TTL": self.ttl,
                    }))
                    response = client.CreateRecord(req)
                    created_id = getattr(response, "RecordId", None)
                    if created_id is None:
                        raise DNSPodError("DNSPod create record returned no RecordId")
                    record_id = str(created_id)
                    steps.append({
                        "key": "create",
                        "label": "创建权限",
                        "status": "passed",
                        "detail": "CreateRecord succeeded",
                    })
                except Exception as exc:
                    steps.append({
                        "key": "create",
                        "label": "创建权限",
                        "status": "failed",
                        "detail": _sdk_error_detail(exc),
                    })
            else:
                steps.append({
                    "key": "create",
                    "label": "创建权限",
                    "status": "skipped",
                    "detail": "Skipped because query permission failed",
                })

            if record_id:
                try:
                    req = models.ModifyRecordRequest()
                    req.from_json_string(json.dumps({
                        "Domain": self.root_domain,
                        "RecordId": int(record_id),
                        "SubDomain": record_name,
                        "RecordType": "A",
                        "RecordLine": self.line,
                        "Value": modified_value,
                        "TTL": self.ttl,
                    }))
                    client.ModifyRecord(req)
                    steps.append({
                        "key": "modify",
                        "label": "修改权限",
                        "status": "passed",
                        "detail": "ModifyRecord succeeded",
                    })
                except Exception as exc:
                    steps.append({
                        "key": "modify",
                        "label": "修改权限",
                        "status": "failed",
                        "detail": _sdk_error_detail(exc),
                    })
            else:
                steps.append({
                    "key": "modify",
                    "label": "修改权限",
                    "status": "skipped",
                    "detail": "Skipped because no temporary record was created",
                })
        finally:
            if create_attempted and not record_id:
                try:
                    record_id = self._find_a_record_id(record_name)
                except Exception as exc:
                    cleanup_ok = False
                    cleanup_lookup_error = _sdk_error_detail(exc)
            if record_id:
                try:
                    req = models.DeleteRecordRequest()
                    req.from_json_string(json.dumps({
                        "Domain": self.root_domain,
                        "RecordId": int(record_id),
                    }))
                    client.DeleteRecord(req)
                    steps.append({
                        "key": "delete",
                        "label": "删除权限",
                        "status": "passed",
                        "detail": "DeleteRecord succeeded; temporary record was cleaned",
                    })
                    record_id = ""
                except Exception as exc:
                    cleanup_ok = False
                    steps.append({
                        "key": "delete",
                        "label": "删除权限",
                        "status": "failed",
                        "detail": _sdk_error_detail(exc),
                    })
            elif cleanup_lookup_error:
                steps.append({
                    "key": "delete",
                    "label": "删除权限",
                    "status": "failed",
                    "detail": (
                        "Unable to verify or clean the temporary record after create failure: "
                        f"{cleanup_lookup_error}"
                    ),
                })
            else:
                steps.append({
                    "key": "delete",
                    "label": "删除权限",
                    "status": "skipped",
                    "detail": "Skipped because no temporary record exists",
                })

        ok = all(step["status"] == "passed" for step in steps)
        first_error = next(
            (step["detail"] for step in steps if step["status"] == "failed"),
            "",
        )
        return {
            "ok": ok,
            "test_domain": test_fqdn,
            "steps": steps,
            "cleanup_ok": cleanup_ok,
            "residual_record": bool(record_id),
            "record_id": record_id,
            "error": first_error,
        }

    def record_name_for(self, fqdn: str) -> str:
        normalized = (fqdn or "").strip().lower().strip(".")
        root = self.root_domain.strip().lower().strip(".")
        if normalized == root:
            return "@"
        suffix = f".{root}"
        if not normalized.endswith(suffix):
            raise DNSPodError(f"domain is outside configured root: {normalized}")
        return normalized[: -len(suffix)]

    def upsert_a_record(
        self,
        fqdn: str,
        value: str,
        record_id: str = "",
    ) -> DNSRecordResult:
        self.ensure_ready()
        record_name = self.record_name_for(fqdn)
        if self.mode == "mock":
            digest = hashlib.sha256(f"{fqdn}:{value}".encode("utf-8")).hexdigest()[:16]
            return DNSRecordResult(f"mock-{digest}", "mock-upsert", self.mode)
        if self.mode == "manual":
            try:
                resolved = {
                    item[4][0]
                    for item in socket.getaddrinfo(fqdn, 443, type=socket.SOCK_STREAM)
                    if item and item[4]
                }
            except OSError as exc:
                raise DNSPodError(f"manual DNS lookup failed for {fqdn}: {exc}") from exc
            if value not in resolved:
                actual = ", ".join(sorted(resolved)) or "no A record"
                raise DNSPodError(
                    f"manual DNS verification failed: {fqdn} resolves to {actual}, expected {value}"
                )
            digest = hashlib.sha256(f"manual:{fqdn}:{value}".encode("utf-8")).hexdigest()[:16]
            return DNSRecordResult(f"manual-{digest}", "manual-verified", self.mode)

        client, models = self._get_sdk()
        resolved_id = (record_id or "").strip() or self._find_a_record_id(record_name)
        if resolved_id:
            req = models.ModifyRecordRequest()
            req.from_json_string(json.dumps({
                "Domain": self.root_domain,
                "RecordId": int(resolved_id),
                "SubDomain": record_name,
                "RecordType": "A",
                "RecordLine": self.line,
                "Value": value,
                "TTL": self.ttl,
            }))
            client.ModifyRecord(req)
            return DNSRecordResult(str(resolved_id), "modified", self.mode)

        req = models.CreateRecordRequest()
        req.from_json_string(json.dumps({
            "Domain": self.root_domain,
            "SubDomain": record_name,
            "RecordType": "A",
            "RecordLine": self.line,
            "Value": value,
            "TTL": self.ttl,
        }))
        response = client.CreateRecord(req)
        created_id = getattr(response, "RecordId", None)
        if created_id is None:
            raise DNSPodError("DNSPod create record returned no RecordId")
        return DNSRecordResult(str(created_id), "created", self.mode)

    def delete_a_record(self, fqdn: str, record_id: str = "") -> DNSRecordResult:
        self.ensure_ready()
        record_name = self.record_name_for(fqdn)
        if self.mode == "mock":
            return DNSRecordResult((record_id or "mock-released"), "mock-delete", self.mode)
        if self.mode == "manual":
            return DNSRecordResult((record_id or "manual-preserved"), "manual-preserved", self.mode)

        client, models = self._get_sdk()
        resolved_id = (record_id or "").strip() or self._find_a_record_id(record_name)
        if not resolved_id:
            return DNSRecordResult("", "not-found", self.mode)
        req = models.DeleteRecordRequest()
        req.from_json_string(json.dumps({
            "Domain": self.root_domain,
            "RecordId": int(resolved_id),
        }))
        client.DeleteRecord(req)
        return DNSRecordResult(str(resolved_id), "deleted", self.mode)

    def _find_a_record_id(self, record_name: str) -> str:
        client, models = self._get_sdk()
        req = models.DescribeRecordListRequest()
        req.from_json_string(json.dumps({
            "Domain": self.root_domain,
            "Subdomain": record_name,
            "RecordType": "A",
            "Limit": 100,
        }))
        try:
            response = client.DescribeRecordList(req)
        except Exception as exc:
            if _sdk_error_code(exc) == _NO_RECORDS_CODE:
                return ""
            raise
        for record in getattr(response, "RecordList", None) or []:
            if getattr(record, "Type", "") == "A":
                record_id = getattr(record, "RecordId", None)
                if record_id is not None:
                    return str(record_id)
        return ""

    def _get_sdk(self):
        if self._client is not None and self._models is not None:
            return self._client, self._models
        try:
            from tencentcloud.common import credential
            from tencentcloud.common.profile.client_profile import ClientProfile
            from tencentcloud.common.profile.http_profile import HttpProfile
            from tencentcloud.dnspod.v20210323 import dnspod_client, models
        except ImportError as exc:
            raise DNSPodError(
                "tencentcloud-sdk-python is required for real DNSPod mode"
            ) from exc

        cred = credential.Credential(self.secret_id, self.secret_key)
        http_profile = HttpProfile()
        http_profile.endpoint = "dnspod.tencentcloudapi.com"
        client_profile = ClientProfile()
        client_profile.httpProfile = http_profile
        self._client = dnspod_client.DnspodClient(cred, "", client_profile)
        self._models = models
        return self._client, self._models
