import json
from pathlib import Path

import jsonschema

_contracts: dict[str, dict] = {}


def load_contracts() -> None:
    contracts_dir = Path(__file__).parent.parent / "spec" / "tool_contracts"
    for f in contracts_dir.glob("*.json"):
        data = json.loads(f.read_text())
        for tool_name, contract in data.get("tools", {}).items():
            _contracts[tool_name] = contract


def validate_arguments(tool_name: str, arguments: dict) -> str | None:
    """Returns an error message if invalid, None if valid."""
    contract = _contracts.get(tool_name)
    if contract is None:
        return f"Unknown tool: '{tool_name}'. No tool contract found."
    try:
        jsonschema.validate(arguments, contract["arguments"])
        return None
    except jsonschema.ValidationError as e:
        return e.message
    except jsonschema.SchemaError as e:
        return f"Tool contract schema error: {e.message}"
