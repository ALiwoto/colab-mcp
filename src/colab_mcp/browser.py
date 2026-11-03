TAB_TOOLS = [
    {
        "name": "list_colab_tabs",
        "description": (
            "List open Colab tabs using the optional Chrome companion. Returns tab IDs, "
            "titles, URLs, and which tab is connected. Open a Colab connection first."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "connect_colab_tab",
        "description": (
            "Move this MCP connection to an existing Colab tab from list_colab_tabs. "
            "The Chrome companion transfers the token automatically without reloading "
            "the target notebook. Does not change its runtime or execute cells. "
            "Finish or cancel file transfers first."
        ),
        "inputSchema": {
            "type": "object", "properties": {"tab_id": {"type": "integer"}},
            "required": ["tab_id"], "additionalProperties": False,
        },
    },
]
TAB_TOOL_NAMES = {tool["name"] for tool in TAB_TOOLS}
