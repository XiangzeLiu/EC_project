import os
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "Server_manager"))
sys.path.insert(0, str(ROOT))

os.environ.setdefault("SM_DNSPOD_MODE", "mock")
os.environ.setdefault("SM_DOMAIN_POOL_REQUIRED", "1")
os.environ.setdefault("SM_LOG_LEVEL", "CRITICAL")

import database
import domain_pool
from dnspod_client import DNSPodClient, DNSPodError
from Client.constants import DEFAULT_SM_BASE_URL
from Trader_Server.config import DEFAULT_MANAGER_URL
from Trader_Server.services.caddy_manager import render_ts_caddyfile
from Trader_Server.services.public_ip import validate_public_ipv4


class ProductionDomainTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        database._DB_PATH = str(Path(self.temp_dir.name) / "sm.db")
        database.init_db()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_domain_pool_registration_and_release(self):
        imported = domain_pool.import_domains([
            f"ts-{index:02d}.ts.scjrdomain.com"
            for index in range(1, 26)
        ])
        self.assertEqual(imported["inserted"], 25)
        page = database.list_ts_domain_pool(page=1, page_size=20)
        self.assertEqual(len(page["items"]), 20)
        self.assertEqual(page["pages"], 2)

        request = database.create_node_request(
            "req_test",
            "test-node",
            "Test",
            host="127.0.0.1:8900",
            public_ip="8.8.8.8",
            source_ip="8.8.8.8",
        )
        self.assertEqual(request["public_ip"], "8.8.8.8")
        assignment = domain_pool.allocate_domain("test-node", "8.8.8.8")
        approved = database.approve_node_request(
            "req_test",
            domain_assignment=assignment,
        )
        self.assertEqual(approved["assigned_domain"], "ts-01.ts.scjrdomain.com")
        self.assertEqual(
            approved["public_endpoint"],
            "wss://ts-01.ts.scjrdomain.com/ws",
        )

        server_id = approved["server_id"]
        self.assertTrue(database.delete_node(server_id))
        released = domain_pool.release_server_domain(server_id)
        self.assertTrue(released["released"])
        self.assertEqual(released["status"], "cooling")

    def test_public_domain_defaults_and_caddy_render(self):
        self.assertEqual(DEFAULT_SM_BASE_URL, "https://scjrdomain.com")
        self.assertEqual(DEFAULT_MANAGER_URL, "https://scjrdomain.com")
        self.assertEqual(validate_public_ipv4("8.8.8.8"), "8.8.8.8")
        with self.assertRaises(ValueError):
            validate_public_ipv4("192.0.2.10")
        caddyfile = render_ts_caddyfile("ts-01.ts.scjrdomain.com")
        self.assertIn("ts-01.ts.scjrdomain.com", caddyfile)
        self.assertIn("127.0.0.1:8900", caddyfile)

    def test_custom_ts_domain_under_root_is_supported(self):
        entry = domain_pool.normalize_domain("www.ts01.scjrdomain.com")
        self.assertEqual(entry["root_domain"], "scjrdomain.com")
        self.assertEqual(entry["record_name"], "www.ts01")
        self.assertEqual(
            entry["public_endpoint"],
            "wss://www.ts01.scjrdomain.com/ws",
        )
        imported = domain_pool.import_domains(["www.ts01.scjrdomain.com"])
        self.assertEqual(imported["inserted"], 1)
        self.assertEqual(imported["errors"], [])
        database.create_node_request(
            "req_custom_domain",
            "ts01-205",
            "TT",
            host="47.239.106.205",
            public_ip="47.239.106.205",
            source_ip="47.239.106.205",
        )
        assignment = domain_pool.allocate_domain("ts01-205", "47.239.106.205")
        approved = database.approve_node_request(
            "req_custom_domain",
            domain_assignment=assignment,
        )
        self.assertEqual(approved["assigned_domain"], "www.ts01.scjrdomain.com")
        self.assertEqual(
            approved["public_endpoint"],
            "wss://www.ts01.scjrdomain.com/ws",
        )
        with self.assertRaises(domain_pool.DomainPoolError):
            domain_pool.normalize_domain("www.ts01.example.com")

    def test_manual_dns_mode_requires_expected_public_ip(self):
        client = DNSPodClient()
        client.mode = "manual"
        with patch("dnspod_client.socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("47.239.106.205", 443)),
        ]):
            result = client.upsert_a_record(
                "www.ts01.scjrdomain.com",
                "47.239.106.205",
            )
        self.assertEqual(result.mode, "manual")
        self.assertEqual(result.action, "manual-verified")
        with patch("dnspod_client.socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("1.1.1.1", 443)),
        ]):
            with self.assertRaises(DNSPodError):
                client.upsert_a_record(
                    "www.ts01.scjrdomain.com",
                    "47.239.106.205",
                )

    def test_concurrent_allocations_are_unique(self):
        domain_pool.import_domains([
            f"ts-{index:02d}.ts.scjrdomain.com"
            for index in range(1, 11)
        ])
        public_ips = (
            "8.8.8.8",
            "1.1.1.1",
            "9.9.9.9",
            "208.67.222.222",
            "4.2.2.2",
        )
        with ThreadPoolExecutor(max_workers=5) as executor:
            assignments = list(executor.map(
                lambda pair: domain_pool.allocate_domain(
                    f"node-{pair[0]}",
                    pair[1],
                ),
                enumerate(public_ips, start=1),
            ))
        assigned_domains = {item["fqdn"] for item in assignments}
        self.assertEqual(len(assigned_domains), len(public_ips))

    def test_concurrent_approval_of_same_request_creates_one_node(self):
        domain_pool.import_domains([
            "ts-01.ts.scjrdomain.com",
            "ts-02.ts.scjrdomain.com",
        ])
        database.create_node_request(
            "req_same",
            "same-node",
            "TT",
            host="8.8.8.8",
            public_ip="8.8.8.8",
            source_ip="8.8.8.8",
        )

        def approve_once(_index):
            assignment = domain_pool.allocate_domain("same-node", "8.8.8.8")
            approved = database.approve_node_request(
                "req_same",
                domain_assignment=assignment,
            )
            if not approved:
                domain_pool.abort_allocation(assignment, "concurrent approval lost")
            return approved

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(approve_once, range(2)))

        self.assertEqual(sum(bool(result) for result in results), 1)
        self.assertEqual(len(database.get_all_nodes()), 1)
        domains = database.list_ts_domain_pool(page_size=20)["items"]
        self.assertEqual(sum(item["status"] == "occupied" for item in domains), 1)
        self.assertEqual(sum(item["status"] == "available" for item in domains), 1)

    def test_orphaned_allocating_domain_can_be_recovered(self):
        domain_pool.import_domains(["ts-01.ts.scjrdomain.com"])
        assignment = domain_pool.allocate_domain("interrupted-node", "8.8.8.8")
        self.assertEqual(
            database.get_ts_domain_pool_entry(assignment["id"])["status"],
            "allocating",
        )
        released = domain_pool.release_orphan_domain(assignment["id"])
        self.assertTrue(released["ok"])
        self.assertEqual(
            database.get_ts_domain_pool_entry(assignment["id"])["status"],
            "available",
        )


if __name__ == "__main__":
    unittest.main()
