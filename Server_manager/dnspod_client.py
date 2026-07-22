"""Tencent Cloud DNSPod A-record operations used by the TS domain pool."""

from __future__ import annotations

import hashlib
import json
import socket
from dataclasses import dataclass

from config import (
    SM_DNSPOD_LINE,
    SM_DNSPOD_MODE,
    SM_DNSPOD_SECRET_ID,
    SM_DNSPOD_SECRET_KEY,
    SM_DNS_TTL,
    SM_DOMAIN_ROOT,
)


class DNSPodError(RuntimeError):
    pass


@dataclass(frozen=True)
class DNSRecordResult:
    record_id: str
    action: str
    mode: str


class DNSPodClient:
    def __init__(self) -> None:
        self.mode = SM_DNSPOD_MODE
        self.root_domain = SM_DOMAIN_ROOT
        self.secret_id = SM_DNSPOD_SECRET_ID
        self.secret_key = SM_DNSPOD_SECRET_KEY
        self.line = SM_DNSPOD_LINE
        self.ttl = SM_DNS_TTL
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
        response = client.DescribeRecordList(req)
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
