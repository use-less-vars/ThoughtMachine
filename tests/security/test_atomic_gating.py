import pytest
from security.security_gate import check_atomic_operation


class TestCheckAtomicOperation:
    def test_allowed_filesystem_write(self):
        assert check_atomic_operation("filesystem:write", {"filesystem": "write"}, "test") is True

    def test_denied_filesystem_read_only(self):
        assert check_atomic_operation("filesystem:write", {"filesystem": "read"}, "test") is False

    def test_ask_treated_as_false(self):
        assert check_atomic_operation("filesystem:write", {"filesystem": "ask"}, "test") is False

    def test_unknown_category_returns_false(self):
        assert check_atomic_operation("unknown:read", {"filesystem": "write"}, "test") is False

    def test_malformed_string_returns_false(self):
        assert check_atomic_operation("no-colon", {}, "test") is False

    def test_empty_permissions_denies(self):
        assert check_atomic_operation("filesystem:write", {}, "test") is False

    def test_bool_permission_true(self):
        assert check_atomic_operation("container:true", {"container": True}, "test") is True

    def test_bool_permission_false(self):
        assert check_atomic_operation("container:true", {"container": False}, "test") is False

    def test_full_satisfies_write(self):
        assert check_atomic_operation("filesystem:write", {"filesystem": "full"}, "test") is True

    def test_network_outbound_allowed(self):
        assert check_atomic_operation("network:outbound", {"network": "write"}, "test") is True

    def test_network_outbound_denied(self):
        assert check_atomic_operation("network:outbound", {"network": "banned"}, "test") is False
