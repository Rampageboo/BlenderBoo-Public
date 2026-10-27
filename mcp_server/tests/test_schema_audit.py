from __future__ import annotations

import unittest

from blenderboo_mcp.schema_audit import audit_schemas


class SchemaAuditTests(unittest.TestCase):
    def test_equivalent_referenced_additional_properties_are_compared(self) -> None:
        addon = {"demo.fill": {"type": "object", "properties": {"fills": {"type": "object",
            "additionalProperties": {"type": "object", "properties": {"ao": {"type": "number", "minimum": 0, "maximum": 1}},
                                     "required": ["ao"], "additionalProperties": False}}},
            "additionalProperties": False}}
        mcp = {"demo.fill": {"type": "object", "properties": {"fills": {"type": "object",
            "additionalProperties": {"$ref": "#/$defs/Channels"}}}, "additionalProperties": False,
            "$defs": {"Channels": {"type": "object", "properties": {"ao": {"type": "number", "minimum": 0, "maximum": 1}},
                                   "required": ["ao"], "additionalProperties": False}}}}
        self.assertTrue(audit_schemas(addon, mcp)["aligned"])
        mcp["demo.fill"]["$defs"]["Channels"]["properties"]["ao"]["maximum"] = 2
        result = audit_schemas(addon, mcp)
        self.assertFalse(result["aligned"])
        self.assertIn("constraint_mismatch", {item["code"] for item in result["issues"]})

    def test_equivalent_optional_mcp_instance_is_ignored(self) -> None:
        addon = {
            "demo.patch": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "minLength": 1},
                    "amount": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "required": ["name"],
                "additionalProperties": False,
            }
        }
        mcp = {
            "demo.patch": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "minLength": 1},
                    "amount": {"anyOf": [{"type": "number", "minimum": 0, "maximum": 1}, {"type": "null"}]},
                    "instance": {"anyOf": [{"type": "string"}, {"type": "integer"}, {"type": "null"}]},
                },
                "required": ["name"],
                "additionalProperties": False,
            }
        }
        result = audit_schemas(addon, mcp)
        self.assertTrue(result["aligned"])
        self.assertEqual(result["issue_count"], 0)

    def test_reports_field_required_type_and_bound_drift(self) -> None:
        addon = {
            "demo.patch": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "minLength": 1},
                    "amount": {"type": "number", "maximum": 1},
                },
                "required": ["name"],
                "additionalProperties": False,
            }
        }
        mcp = {
            "demo.patch": {
                "type": "object",
                "properties": {
                    "name": {"type": "integer"},
                    "amount": {"type": "number", "maximum": 2},
                    "extra": {"type": "boolean"},
                },
                "required": ["amount"],
                "additionalProperties": True,
            }
        }
        result = audit_schemas(addon, mcp)
        self.assertFalse(result["aligned"])
        codes = {issue["code"] for issue in result["issues"]}
        self.assertIn("type_mismatch", codes)
        self.assertIn("constraint_mismatch", codes)
        self.assertIn("extra_mcp_field", codes)
        self.assertIn("required_mismatch", codes)
        self.assertIn("additional_properties_mismatch", codes)

    def test_expected_mcp_only_tools_are_not_drift(self) -> None:
        result = audit_schemas(
            {},
            {"adapter.schema_audit": {"type": "object", "properties": {}}},
            mcp_only={"adapter.schema_audit"},
        )
        self.assertTrue(result["aligned"])

    def test_reports_missing_array_item_contract(self) -> None:
        result = audit_schemas(
            {
                "demo.patch": {
                    "type": "object",
                    "properties": {
                        "values": {"type": "array", "items": {"type": "number"}},
                    },
                    "additionalProperties": False,
                }
            },
            {
                "demo.patch": {
                    "type": "object",
                    "properties": {"values": {"type": "array"}},
                    "additionalProperties": False,
                }
            },
        )
        self.assertFalse(result["aligned"])
        self.assertIn("array_items_mismatch", {issue["code"] for issue in result["issues"]})

    def test_nullable_array_branch_is_compared_to_homogeneous_items(self) -> None:
        result = audit_schemas(
            {
                "demo.patch": {
                    "type": "object",
                    "properties": {
                        "value": {
                            "type": "array",
                            "items": {"type": "number"},
                            "minItems": 3,
                            "maxItems": 3,
                        }
                    },
                    "additionalProperties": False,
                }
            },
            {
                "demo.patch": {
                    "type": "object",
                    "properties": {
                        "value": {
                            "anyOf": [
                                {
                                    "type": "array",
                                    "prefixItems": [
                                        {"type": "number"},
                                        {"type": "number"},
                                        {"type": "number"},
                                    ],
                                    "minItems": 3,
                                    "maxItems": 3,
                                },
                                {"type": "null"},
                            ]
                        }
                    },
                    "additionalProperties": False,
                }
            },
        )
        self.assertTrue(result["aligned"], result["issues"])

    def test_absent_and_true_additional_properties_are_equivalent(self) -> None:
        result = audit_schemas(
            {"demo.patch": {"type": "object", "properties": {}}},
            {"demo.patch": {"type": "object", "properties": {}, "additionalProperties": True}},
        )
        self.assertTrue(result["aligned"], result["issues"])

    def test_required_array_item_fields_are_compared(self) -> None:
        result = audit_schemas(
            {
                "demo.patch": {
                    "type": "object",
                    "properties": {
                        "operations": {
                            "type": "array",
                            "items": {"type": "object", "required": ["op"]},
                        }
                    },
                    "additionalProperties": False,
                }
            },
            {
                "demo.patch": {
                    "type": "object",
                    "properties": {
                        "operations": {
                            "type": "array",
                            "items": {"type": "object"},
                        }
                    },
                    "additionalProperties": False,
                }
            },
        )
        self.assertFalse(result["aligned"])
        self.assertIn("required_mismatch", {issue["code"] for issue in result["issues"]})

    def test_issue_output_is_bounded_without_losing_counts(self) -> None:
        addon = {
            f"demo.tool_{index}": {"type": "object", "properties": {}, "additionalProperties": False}
            for index in range(5)
        }
        result = audit_schemas(addon, {}, max_issues=2)
        self.assertEqual(result["issue_count"], 5)
        self.assertEqual(len(result["issues"]), 2)
        self.assertTrue(result["issues_truncated"])


if __name__ == "__main__":
    unittest.main()
